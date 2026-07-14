# ----- Python 표준 라이브러리
import gc
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta
import builtins
import warnings

# ----- 서드파티
try:
    import boto3  # S3 업로드 전용 (사내 환경). 로컬/오프라인에서는 없을 수 있음 → graceful skip
except ImportError:
    boto3 = None
    print("[WARN] boto3 미설치 - S3 업로드 비활성화 (로컬 테스트 모드)")
import duckdb
import numpy as np
import pandas as pd
import requests

# ----- 프로젝트 내부 모듈
# NOTE: bigdataquery는 Main에서 직접 쓰지 않으므로 import하지 않는다.
#   (병렬 렌더링 워커가 __main__=Main을 재import할 때 무거운 bigdataquery 재import·안내문
#    출력이 매번 발생하던 문제 방지 — 실제 쿼리는 My_Function 내부에서 지연 import한다.)
from My_Function import *
from My_config import GLOBAL_CONFIG
from anomaly_engine import analyze_commonality, render_findings_html, render_findings_count_html, interpret_with_ai, item_excluded, compile_nl_to_json

# ==================================================================================================================================
# GPT OSS 120B API 연결 설정
# ==================================================================================================================================

from openai import OpenAI


# ==================================================================================================================================

warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
# openpyxl "Conditional Formatting extension is not supported" 등 UserWarning 억제
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
warnings.filterwarnings("ignore", message=".*Conditional Formatting extension is not supported.*")
warnings.filterwarnings("ignore", message=".*extension is not supported and will be removed.*")
# matplotlib: 기본 폰트(DejaVu Sans)가 U+2212(minus sign) 등 일부 글리프를 못 그릴 때 나오는
#   "Glyph ... missing from font(s)" 경고 억제. (warn_on_missing_glyph → warnings.warn(UserWarning))
#   각 차트 함수에서 axes.unicode_minus=False를 이미 설정하지만, 렌더 텍스트에 실제 U+2212가 섞이면
#   경고가 계속 나므로 메시지 패턴으로 무음 처리. Windows spawn 워커는 __main__(Main)을 재import하므로
#   이 필터가 렌더링 워커 프로세스에도 그대로 적용된다.
warnings.filterwarnings("ignore", message=".*Glyph.*")
# matplotlib "findfont: Font family 'NanumGothic' not found." 로그 억제.
#   이 메시지는 warnings가 아니라 logging(matplotlib.font_manager 로거, WARNING 레벨)으로
#   나오므로 위 필터로는 안 잡힌다. 설정 폰트가 미설치인 환경에서 차트 텍스트를 그릴 때마다
#   반복 출력돼 로그를 어지럽힘 → 로거 레벨을 ERROR로 올려 무음 처리(렌더링엔 영향 없음).
import logging as _mpl_logging
_mpl_logging.getLogger('matplotlib.font_manager').setLevel(_mpl_logging.ERROR)


# ==================================================================================================================================


# ==================================================================================================================================
# 실행 로그/터미널 출력 인프라
#  - 모든 print를 가로채 통합 로그(제품명_log.txt)에 시간순 append + 30MB 초과 시 오래된(앞) 내용 자동 삭제.
#  - 터미널: [ERROR]/[WARN]은 자동 색 강조, 중요 마일스톤은 print_status()로 초록/파랑/빨강 강조.
#  - 로그 파일에는 ANSI 색코드를 제거하고 기록.
# ==================================================================================================================================
_original_print = builtins.print
_LOG_PATH = None
_LOG_MAX_BYTES = 30 * 1024 * 1024
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

# Windows 콘솔에서 ANSI 색상(VT) 활성화 (미지원 환경이면 무해하게 skip)
if os.name == 'nt':
    try:
        import ctypes as _ctypes
        _k = _ctypes.windll.kernel32
        _k.SetConsoleMode(_k.GetStdHandle(-11), 7)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING 포함
    except Exception:
        pass

# ANSI 색상 (터미널 강조용)
_COL = {'reset': '\x1b[0m', 'green': '\x1b[92m', 'blue': '\x1b[94m', 'red': '\x1b[91m',
        'yellow': '\x1b[93m', 'cyan': '\x1b[96m', 'bold': '\x1b[1m'}

def _c(text, color):
    return f"{_COL.get(color, '')}{text}{_COL['reset']}"

def _safe_console_print(text, **kwargs):
    """콘솔 인코딩(cp949 등)이 표현 못하는 문자가 있어도 죽지 않게 출력."""
    try:
        _original_print(text, **kwargs)
    except UnicodeEncodeError:
        _enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        _original_print(text.encode(_enc, errors='replace').decode(_enc, errors='replace'), **kwargs)

def _rotate_unified_log():
    """통합 로그가 30MB를 넘으면 오래된(앞) 내용을 버리고 최신 ~24MB만 유지."""
    keep = 24 * 1024 * 1024
    try:
        with open(_LOG_PATH, 'rb') as f:
            f.seek(0, os.SEEK_END); size = f.tell()
            if size <= _LOG_MAX_BYTES:
                return
            f.seek(size - keep); data = f.read()
        nl = data.find(b'\n')
        if nl != -1:
            data = data[nl + 1:]
        with open(_LOG_PATH, 'wb') as f:
            f.write(data)
    except Exception:
        pass

def _run_log_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    # 터미널: 색이 없고 [ERROR]/[FAIL]/[WARN]이면 자동 강조
    term = msg
    if '\x1b[' not in msg:
        _s = msg.lstrip()
        if _s.startswith('[ERROR]') or _s.startswith('[FAIL]'):
            term = _c(msg, 'red')
        elif _s.startswith('[WARN]'):
            term = _c(msg, 'yellow')
    _safe_console_print(term, **kwargs)
    # 통합 로그 파일: ANSI 제거 후 시간순 append + rotation
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {_ANSI_RE.sub('', msg)}\n")
            _rotate_unified_log()
        except Exception:
            pass

def print_status(category, state, detail=''):
    """중요 상태를 색으로 강조 출력. 마커는 cp949 콘솔 호환 위해 ASCII 사용.
    state: ok(초록)/fail(빨강)/info(파랑)/skip(노랑)/on(초록)/off(노랑)."""
    tag, color = {'ok': ('[ OK ]', 'green'), 'fail': ('[FAIL]', 'red'), 'info': ('[ >> ]', 'blue'),
                  'skip': ('[SKIP]', 'yellow'), 'on': ('[ ON ]', 'green'), 'off': ('[ OFF]', 'yellow')
                  }.get(state, ('[ -- ]', 'cyan'))
    print(_c(f"{tag} {category}" + (f": {detail}" if detail else ""), color))


def _slide_title(slide):
    """슬라이드의 첫 비어있지 않은 텍스트(제목)를 반환."""
    for sh in slide.shapes:
        try:
            if sh.has_text_frame and sh.text_frame.text.strip():
                return sh.text_frame.text.strip()
        except Exception:
            pass
    return ""


def _save_rule_check_log(ai_dir, lot_id, step_id, rule_trace, findings):
    """전체 anomaly rule 체크 결과를 RUN/AI 폴더에 파일로 저장.

    모든 [RULE] 규칙(체이닝/산포억제/산포비교)을 순회한 매칭/해당없음 전량을 기록하고,
    사람이 읽는 .txt(요약+표)와 기계용 .json(rule_trace 원본) 2개를 남긴다.
    파일명: anomaly_rule_check_{lot}_{step_id}.(txt|json) — 리포트 키({lot}_{step_id},
    원본 step_id 기준)와 동일 체계. (AI 인풋 폴더 = 사이클 정리 대상 아님)
    """
    import json as _json
    try:
        os.makedirs(ai_dir, exist_ok=True)
    except Exception:
        pass
    _safe = lambda s: re.sub(r'[^0-9A-Za-z가-힣._-]+', '_', str(s or 'NA'))
    base = f"anomaly_rule_check_{_safe(lot_id)}_{_safe(step_id)}"
    trace = rule_trace or []
    n_all = len(trace)
    n_hit = sum(1 for t in trace if t.get('matched'))
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = []
    lines.append("=" * 78)
    lines.append(f"Anomaly Rule Check 결과  (LOT={lot_id}  STEP_ID={step_id})")
    lines.append(f"생성시각: {ts}")
    lines.append(f"전체 규칙 {n_all}개 체크 — 매칭 {n_hit}건 / 해당없음 {n_all - n_hit}건")
    lines.append("=" * 78)
    if trace:
        lines.append("")
        lines.append("[매칭된 규칙]")
        _hit = [t for t in trace if t.get('matched')]
        if _hit:
            for t in _hit:
                lines.append(f"  ● [{t.get('kind','')}] {t.get('name','')}")
                lines.append(f"       조건: {t.get('cond','')}")
                lines.append(f"       결과: {t.get('result','')}")
                if t.get('note'):
                    lines.append(f"       비고: {t.get('note','')}")
        else:
            lines.append("  (매칭된 규칙 없음)")
        lines.append("")
        lines.append("[해당없음(미매칭) 규칙]")
        _miss = [t for t in trace if not t.get('matched')]
        if _miss:
            for t in _miss:
                lines.append(f"  · [{t.get('kind','')}] {t.get('name','')} — {t.get('result','')}  |  조건: {t.get('cond','')}")
        else:
            lines.append("  (미매칭 규칙 없음)")
    else:
        lines.append("")
        lines.append("정의된 anomaly rule 없음(체크 대상 0개).")
    # 최종 finding 요약(참고)
    lines.append("")
    lines.append("-" * 78)
    lines.append(f"[최종 Finding 요약] 총 {len(findings or [])}건")
    for f in (findings or []):
        lines.append(f"  · [{f.get('severity','')}/{f.get('type','')}] {f.get('title','')}")

    txt_path = os.path.join(ai_dir, base + '.txt')
    json_path = os.path.join(ai_dir, base + '.json')
    with open(txt_path, 'w', encoding='utf-8') as fh:
        fh.write("\n".join(lines) + "\n")
    with open(json_path, 'w', encoding='utf-8') as jf:
        _json.dump({'lot_id': lot_id, 'step_id': step_id, 'generated': ts,
                    'n_rules': n_all, 'n_matched': n_hit, 'rule_trace': trace,
                    'findings': [{'severity': f.get('severity'), 'type': f.get('type'),
                                  'title': f.get('title'), 'item': f.get('item')}
                                 for f in (findings or [])]},
                   jf, ensure_ascii=False, indent=2)
    print(f"[RULE CHECK] 결과 저장: RUN/AI/{base}.txt (+.json) — 규칙 {n_all}개(매칭 {n_hit})")


def _save_archive_snapshot(report_key, meta, findings, item_stats, rule_trace,
                           target_rows=None, index_items=None):
    """발행 스냅샷을 RUN/ARCHIVE/<report_key>/에 저장 — 규칙 제안 다이제스트·확정 사례 아카이브 입력.

    - summary.json        : 발행 메타(generated_at 포함) + findings + item_stats + rule_trace.
    - target_rows.parquet : target lot 측정 rows 중 '발행 당시 REPORT ORDER index' 컬럼만(+좌표 메타)
                            — 이후 reformatter/ADDP가 바뀌어도 당시 값이 고정 보존.
    스냅샷은 부가 산출물: 읽는 기능은 파일이 지워져 있어도 동작해야 하고, 저장 실패도
    리포트 발행에 영향을 주지 않는다(호출부 try/except).
    """
    import json as _json
    _dir = os.path.join('RUN', 'ARCHIVE', re.sub(r'[^0-9A-Za-z가-힣._-]+', '_', str(report_key)))
    os.makedirs(_dir, exist_ok=True)
    with open(os.path.join(_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        _json.dump({**meta, 'findings': findings, 'item_stats': item_stats,
                    'rule_trace': rule_trace}, f, ensure_ascii=False, indent=2, default=str)
    n_rows = 0
    if target_rows is not None and len(target_rows) > 0 and index_items:
        _meta_cols = [c for c in ('FAB_LOT_ID', 'WAFER_ID', 'CHIP_X_ADJ', 'CHIP_Y_ADJ',
                                  'TKOUT_TIME', 'PGM(pt)') if c in target_rows.columns]
        _item_cols = [c for c in target_rows.columns if c in set(index_items)]
        if _item_cols:
            _snap = target_rows[_meta_cols + _item_cols]
            _snap.to_parquet(os.path.join(_dir, 'target_rows.parquet'), index=False)
            n_rows = len(_snap)
    print(f"[archive] 발행 스냅샷 저장: {_dir} (summary.json + rows {n_rows})")


def _maybe_send_rule_digest(json_rules, llm_fn, force=False):
    """규칙 제안 다이제스트를 1일 1회 생성/발송 — POWER_USER 대상, 승인 여부와 무관하게 매일 반복 제안.

    - 생성: anomaly_engine.build_rule_digest(RUN/ARCHIVE 집계) → RUN/AI/rule_digest_<날짜>.txt 저장.
    - 발송: 메일링 xlsx에 'POWER_USER' 시트가 있고 use_email_send=True일 때만
      (시트 존재를 직접 확인 — get_email_list의 기본 그룹 fallback으로 전체 오발송하지 않도록).
    - 상태: RUN/AI/rule_digest_state.json(last_sent)으로 1일 1회 보장(force=True는 재발송).
    스냅샷/규칙/수신처가 없어도 파일 저장까지는 정상 동작. 예외는 호출부에서 무시(발행 무영향).
    """
    import json as _json
    import html as _html
    from anomaly_engine import build_rule_digest
    if not getattr(GLOBAL_CONFIG, 'rule_digest_enabled', False):
        return
    _ai_dir = os.path.join('RUN', 'AI')
    os.makedirs(_ai_dir, exist_ok=True)
    _state_p = os.path.join(_ai_dir, 'rule_digest_state.json')
    _today = datetime.now().strftime('%Y-%m-%d')
    if not force:
        try:
            with open(_state_p, encoding='utf-8') as f:
                if _json.load(f).get('last_sent') == _today:
                    return
        except Exception:
            pass   # 상태 파일 없음/손상 → 오늘 미발송으로 간주
    d = build_rule_digest(json_rules=json_rules, llm_fn=llm_fn,
                          window_days=getattr(GLOBAL_CONFIG, 'rule_digest_window_days', 14),
                          min_repeat=getattr(GLOBAL_CONFIG, 'rule_digest_min_repeat', 3))
    _out = os.path.join(_ai_dir, f"rule_digest_{_today.replace('-', '')}.txt")
    with open(_out, 'w', encoding='utf-8') as f:
        f.write(d['text'])
    print(f"[digest] 규칙 다이제스트 저장: {_out} (리포트 {d['n_reports']}건 집계 · "
          f"규칙 {d['n_rules']}개 · 제안 {d['n_proposals']}건 · 좌표재발 {d.get('n_coord', 0)}건)")
    _sent_note = ''
    try:
        _elp = GLOBAL_CONFIG.get('email_list_path')
        if not getattr(GLOBAL_CONFIG, 'use_email_send', False):
            _sent_note = 'use_email_send=False → 파일만 저장'
        elif not (_elp and os.path.exists(_elp) and 'POWER_USER' in pd.ExcelFile(_elp).sheet_names):
            _sent_note = '메일링 xlsx에 POWER_USER 시트 없음 → 파일만 저장'
        else:
            _rcv = get_email_list(_elp, 'POWER_USER')
            _payload_content = {
                'content': ('<pre style="font-family:Consolas,Menlo,monospace; font-size:13px;">'
                            + _html.escape(d['text']) + '</pre>'),
                'receiverList': _rcv,
                'senderMailAddress': f"{GLOBAL_CONFIG.get('KNOXID')}@samsung.com",
                'statusCode': 'SENT',
                'title': (f"[HOL] 규칙 제안 다이제스트 {_today} "
                          f"(리포트 {d['n_reports']}건 · 제안 {d['n_proposals']}건)"),
            }
            # 사내 메일 API(/send/attach)는 multipart/form-data를 요구한다. 첨부(PPT)가
            # 있는 리포트 발송은 files=[...] 덕에 자동으로 multipart가 되지만, 첨부가 없는
            # 다이제스트는 data= 만 쓰면 application/x-www-form-urlencoded로 전송돼
            # 서버가 content type 오류(HTTP 500)를 낸다. → mailSendString을 multipart
            # form-data 파트(None=파일 아님)로 보내 리포트 발송과 동일한 Content-Type 사용.
            _resp = requests.request('POST', GLOBAL_CONFIG.get('url'),
                                     headers={'x-dep-ticket': GLOBAL_CONFIG.get('TICKET')},
                                     files=[('mailSendString', (None, f'{_payload_content}'))])
            _sc = getattr(_resp, 'status_code', None)
            _sent_note = f"POWER_USER {len(_rcv)}명 발송(HTTP {_sc})"
            if _sc != 200:
                print(f"[ERROR] 다이제스트 발송 응답 오류 (HTTP {_sc}) 상세: {getattr(_resp, 'text', '')}")
    except Exception as _me:
        _sent_note = f'발송 실패: {_me}'
    print(f"[digest] {_sent_note}")
    with open(_state_p, 'w', encoding='utf-8') as f:
        _json.dump({'last_sent': _today, 'note': _sent_note}, f, ensure_ascii=False)


def _move_aggregation_after_scoreboard(prs):
    """Index Aggregation Table(통계표) 슬라이드를 Score Board 슬라이드 바로 뒤로 이동
    → 'Score Board → 통계표' 순서로 인접 배치."""
    try:
        slides = list(prs.slides)
        titles = [_slide_title(s) for s in slides]
        sb_idx = [i for i, t in enumerate(titles) if t.startswith('Score Board')]
        agg_idx = [i for i, t in enumerate(titles) if t.startswith('Index Aggregation Table')]
        if not sb_idx or not agg_idx:
            return
        last_sb = max(sb_idx)
        sldIdLst = prs.slides._sldIdLst
        els = list(sldIdLst)
        sb_el = els[last_sb]
        agg_els = [els[i] for i in agg_idx]
        for _e in agg_els:
            sldIdLst.remove(_e)
        _pos = list(sldIdLst).index(sb_el) + 1
        for _off, _e in enumerate(agg_els):
            sldIdLst.insert(_pos + _off, _e)
    except Exception as _e:
        print(f"[WARN] Aggregation 통계표 위치 이동 실패: {_e}")


# ==================================================================================================================================
# 전체 파이프라인 진입점
#   ⚠️ 병렬 차트 렌더링(My_Function의 ProcessPoolExecutor, Windows spawn)이 워커 프로세스에서
#   __main__(이 파일)을 다시 import 하므로, 실행 본문은 반드시 main() + __main__ 가드 안에
#   있어야 한다. (가드가 없으면 워커가 뜰 때마다 쿼리/리포트 발행이 재실행된다.)
# ==================================================================================================================================
def main():
    global _LOG_PATH

    # API 설정 (.env 에서 로드, 없으면 None)
    GPT_API_BASE_URL = os.getenv("GPT_API_BASE_URL")
    GPT_CREDENTIAL_KEY = os.getenv("GPT_CREDENTIAL_KEY")

    # 연결 상태 플래그
    GPT_CONNECT = False
    gpt_client = None

    if GPT_API_BASE_URL and GPT_CREDENTIAL_KEY:
        try:
            gpt_client = OpenAI(
                api_key="dummy",
                base_url=GPT_API_BASE_URL,
                default_headers={
                    "x-dep-ticket": GPT_CREDENTIAL_KEY,
                    "Send-System-Name": "playground",
                    "User-Id": GLOBAL_CONFIG.get("GPT_USER_ID"),
                    "User-Type": "AD_ID",
                    "Prompt-Msg-Id": str(uuid.uuid4()),
                    "Completion-Msg-Id": str(uuid.uuid4()),
                },
            )
            # 연결 테스트 (간단한 메시지 전송)
            gpt_client.chat.completions.create(
                model="gpt-oss-120b",
                messages=[{"role": "user", "content": "Hi"}],
                temperature=0.5,
            )
            GPT_CONNECT = True
            print_status("GPT 연결", "ok", "성공 (gpt-oss-120b)")
        except Exception as e:
            print_status("GPT 연결", "fail", f"실패: {e}")
            gpt_client = None
    else:
        print_status("GPT 연결", "skip", "미설정(.env GPT_API_BASE_URL/GPT_CREDENTIAL_KEY) → AI 해석 비활성")

    def _gpt_chat(system, user):
        """gpt-oss-120b 1회 호출 → 최종 텍스트. AI 해석/NL 규칙 변환 공용 transport.

        실환경(사내 게이트웨이) 견고화 2가지 — 'GPT 연결 성공인데 정리 문구가 비는' 원인 차단:
        ① Prompt/Completion-Msg-Id를 **요청마다 새로** 발급(extra_headers). client 생성 시
           default_headers에 고정하면 모든 호출이 같은 msg-id로 나가 게이트웨이가 중복으로
           보고 빈/캐시 응답을 줄 수 있다(연결 테스트 1회만 성공하는 증상).
        ② reasoning 모델 응답 흡수 — 서버 설정에 따라 최종 답이 content가 아니라
           reasoning_content에 오거나, content에 harmony 채널 마크업이 섞여 온다.
           content가 비면 reasoning_content로 폴백하고, 채널 마크업은 final 채널만 취한다.
        """
        r = gpt_client.chat.completions.create(
            model="gpt-oss-120b",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.3,
            extra_headers={"Prompt-Msg-Id": str(uuid.uuid4()),
                           "Completion-Msg-Id": str(uuid.uuid4())})
        try:
            msg = r.choices[0].message
        except Exception:
            return ""
        txt = getattr(msg, 'content', None) or ''
        if not str(txt).strip():   # 최종 답이 reasoning 필드에 온 경우
            txt = (getattr(msg, 'reasoning_content', None)
                   or getattr(msg, 'reasoning', None) or '')
        txt = str(txt)
        if '<|channel|>final<|message|>' in txt:   # harmony 마크업 → final 채널만
            txt = txt.split('<|channel|>final<|message|>')[-1]
        for _tok in ('<|end|>', '<|return|>', '<|call|>'):
            txt = txt.split(_tok)[0]
        return txt.strip()

    if len(sys.argv) != 2:
        print("Usage: python main.py <ItemName>")
        sys.exit(1)

    raw_arg = sys.argv[1]
    trigger_flag = False

    # ── CLI: 자연어 규칙 변환 도구 (리포트 생성과 별개) ──
    #   python Main.py --convert-nl-rules      : 변환 결과(자연어→when) 미리보기 + 매핑 캐시 갱신(발행/MD 변경 없음)
    #   python Main.py --convert-nl-rules-md   : 변환해서 '바로' MD의 ANOMALY_RULES에 [RULE]로 적용(확인 없음)
    #   AI(GPT OSS 120B) 연결 시 AI 변환, 미연결 시 키워드 fallback. 같은 문구는 캐시로 항상 같은 코드.
    # ── CLI: 규칙 제안 다이제스트 미리보기 (리포트 발행과 별개, 상태 파일/메일 발송 없음) ──
    #   RUN/ARCHIVE 스냅샷을 집계해 터미널에 출력. 실제 저장/발송은 발행 루프 말미에 1일 1회 자동.
    if raw_arg == '--rule-digest':
        from anomaly_engine import build_rule_digest
        _llm = _gpt_chat if (GPT_CONNECT and gpt_client is not None) else None
        _kp = GLOBAL_CONFIG.get("anomaly_knowledge_path")
        _kt = ''
        if _kp and os.path.exists(_kp):
            with open(_kp, encoding='utf-8') as _kf:
                _kt = _kf.read()
        _rules = compile_nl_to_json(_kt, _llm, cache_dir=os.path.join('RUN', 'AI')) if _kt else []
        _d = build_rule_digest(json_rules=_rules, llm_fn=_llm,
                               window_days=getattr(GLOBAL_CONFIG, 'rule_digest_window_days', 14),
                               min_repeat=getattr(GLOBAL_CONFIG, 'rule_digest_min_repeat', 3))
        # 콘솔 인코딩(cp949 등)에 없는 문자는 ?로 치환해 출력(통합 print 훅 설치 전 단계)
        _enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        print(_d['text'].encode(_enc, errors='replace').decode(_enc))
        sys.exit(0)

    if raw_arg in ('--convert-nl-rules', '--convert-nl-rules-md'):
        from anomaly_engine import preview_nl_rules, apply_nl_rules_to_md

        _llm = _gpt_chat if (GPT_CONNECT and gpt_client is not None) else None
        print("[NL] 변환 방식:", "AI(gpt-oss-120b)" if _llm else "키워드 fallback(AI 미연결)")
        _kp = GLOBAL_CONFIG.get("anomaly_knowledge_path")
        if not (_kp and os.path.exists(_kp)):
            print(f"[NL] anomaly_knowledge_path를 찾을 수 없습니다: {_kp}")
            sys.exit(1)
        _cd = os.path.join('RUN', 'AI')
        if raw_arg.endswith('-md'):
            _ok = apply_nl_rules_to_md(_kp, llm_fn=_llm, cache_dir=_cd)
        else:
            _ok = preview_nl_rules(_kp, llm_fn=_llm, cache_dir=_cd)
        sys.exit(0 if _ok else 2)

    # (TRIGGER) 제거
    if raw_arg.startswith("_TRIGGER_"):
        raw_arg = raw_arg.replace("_TRIGGER_", "", 1)
        # rsplit to handle vehicle names with underscores like 'vehicle_A'
        parts = raw_arg.strip().rsplit("_", 2)
        vehicle_name = parts[0]
        trigger_flag = True
    else :
        vehicle_name = raw_arg

    # config.yaml에서 설정 로드
    GLOBAL_CONFIG.load_from_yaml(vehicle_name)

    # =============================================== Config get ==================================================================

    vehicle = GLOBAL_CONFIG.get("vehicle")
    inline_file_sheet = GLOBAL_CONFIG.get("inline_file_sheet")

    prod = GLOBAL_CONFIG.get("prod")
    with_vehicle = GLOBAL_CONFIG.get("with_vehicle")
    delay_min = GLOBAL_CONFIG.get("delay_min")
    viewing_period = int(GLOBAL_CONFIG.get("viewing_period"))
    et_log_show = GLOBAL_CONFIG.get("et_log_show")
    test_mode = GLOBAL_CONFIG.get("test_mode")
    DB_Setting_mode = GLOBAL_CONFIG.get("DB_Setting_mode")
    KNOXID = GLOBAL_CONFIG.get("KNOXID")
    email_receiver = GLOBAL_CONFIG.get("email_receiver")

    ROOT = GLOBAL_CONFIG.get("ROOT")
    DB = GLOBAL_CONFIG.get("DB")
    DB_et_daily = GLOBAL_CONFIG.get("DB_et_daily")
    Report = GLOBAL_CONFIG.get("Report")
    low_qual_ppt_save_path = GLOBAL_CONFIG.get("low_qual_ppt_save_path")
    html_save_path = GLOBAL_CONFIG.get("html_save_path")

    inline_file_path = GLOBAL_CONFIG.get("inline_file_path")
    coordinate_file_path = GLOBAL_CONFIG.get("coordinate_file_path")
    description_ppt_path = GLOBAL_CONFIG.get("description_ppt_path")
    email_list_path = GLOBAL_CONFIG.get("email_list_path")

    log = GLOBAL_CONFIG.get("log")
    query_log = GLOBAL_CONFIG.get("query_log")
    loop_log = GLOBAL_CONFIG.get("loop_log")
    error_log = GLOBAL_CONFIG.get("error_log")
    et_log_path = GLOBAL_CONFIG.get("et_log_path")
    Final_et_log_path = GLOBAL_CONFIG.get("Final_et_log_path")
    report_making = GLOBAL_CONFIG.get("report_making")
    ptype_lot_turnoff = GLOBAL_CONFIG.get("ptype_lot_turnoff")
    specific_dc_layer = GLOBAL_CONFIG.get("specific_dc_layer")

    # =============================================== Folder path 생성 ==================================================================
    # NOTE: DB_et_LOTWF_raw / DB_et_LOTWF_pivot_raw 삭제됨 — daily DB에서 DuckDB로 직접 조회

    # RUN/AI = AI 인풋파일 보관 폴더(사이클 정리 대상 아님), RUN/TEMP = 임시 산출물 폴더
    _ai_dir = os.path.join(ROOT, 'AI')
    _temp_dir = os.path.join(ROOT, 'TEMP')
    for target_path in [ROOT, DB, DB_et_daily, log, Report, low_qual_ppt_save_path, html_save_path, _ai_dir, _temp_dir]:
        if not os.path.exists(target_path):
            os.makedirs(target_path)

    # 통합 로그 print 후킹 초기화 — 모든 로그를 제품명_log.txt 하나로(시간순 append, 30MB rotation)
    _LOG_PATH = GLOBAL_CONFIG.get("unified_log") or loop_log
    builtins.print = _run_log_print

    # 실행 환경(CPU 코어 수 / 가용 메모리) 인식·출력
    _cores = os.cpu_count() or 0
    try:
        import psutil as _ps
        _vm = _ps.virtual_memory()
        _mem_msg = f"메모리 가용 {_vm.available / 1024**3:.1f} GB / 총 {_vm.total / 1024**3:.1f} GB"
    except Exception:
        try:
            from My_Function import _get_available_mem_gb
            _a = _get_available_mem_gb()
            _mem_msg = f"메모리 가용 {_a:.1f} GB" if _a else "메모리 측정 불가"
        except Exception:
            _mem_msg = "메모리 측정 불가"
    print_status("실행 환경", "info", f"CPU {_cores} cores / {_mem_msg}")

    # =============================================== Main Loop 실행 ====================================================================
    bucket_dx = GLOBAL_CONFIG.get("bucket_dx")

    # S3 client (사내 환경 전용 - 로컬에서는 graceful skip)
    _use_s3 = getattr(GLOBAL_CONFIG, 'use_s3_upload', True)
    S3_CONNECT = False
    client = None
    if not _use_s3:
        print_status("S3 드라이브", "off", "use_s3_upload=False → 업로드 비활성")
    elif boto3 is None:
        print_status("S3 드라이브", "off", "boto3 미설치(로컬) → 업로드 비활성")
    else:
        try:
            client = boto3.client(
                        service_name='s3', region_name='DS',
                        aws_access_key_id=GLOBAL_CONFIG.get("s3_aws_access_key_id"),
                        aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),
                        endpoint_url=GLOBAL_CONFIG.get("endpoint_url"))
            S3_CONNECT = True
            print_status("S3 드라이브", "on", "client 연결 성공")
        except Exception as s3_init_err:
            print_status("S3 드라이브", "fail", f"client 초기화 실패: {s3_init_err}")

    datetime_now = datetime.now()
    upload_date = datetime_now.strftime('%Y%m%d')

    # ── AI 다단계 해석용: LLM 호출 함수(transport)와 지식베이스 텍스트를 1회 구성 ──
    # 실 환경에서는 gpt_client(OpenAI)를 사용, 로컬에서는 gpt_oss_client.mock_llm 사용.
    # 둘 다 없으면 None → AI 해석 비활성(코드 분석만).
    def _build_llm_fn():
        if GPT_CONNECT and gpt_client is not None:
            return _gpt_chat   # 공용 transport(요청별 msg-id + reasoning 응답 흡수)
        try:
            from gpt_oss_client import mock_llm
            return mock_llm
        except Exception:
            return None

    _LLM_FN = _build_llm_fn()
    _ANOMALY_KNOWLEDGE_TEXT = ""
    try:
        _kp = GLOBAL_CONFIG.get("anomaly_knowledge_path")
        if _kp and os.path.exists(_kp):
            with open(_kp, encoding="utf-8") as _kf:
                _ANOMALY_KNOWLEDGE_TEXT = _kf.read()
    except Exception as _ke:
        print(f"[WARN] 이상 지식베이스 로드 실패: {_ke}")

    # ── [RULE] 규칙은 아래 NL→JSON 단일 엔진으로만 판정한다 ──
    #   NL_RULES 마커의 `[RULE]` 한 줄들을 JSON 조건으로 변환해 evaluate_json_rules로 판정한다.
    #   판정 방식: '모든 [RULE]을 전부 점검 → 조건 만족하는 규칙마다 각각 코멘트' (다중 매칭 전부 표기).
    #   (구 verbose [RULE] 체이닝(먼저 만족한 분기 1개만) 컴파일 경로는 중복 판정 방지를 위해 비활성화.)

    # ── 자연어 규칙 → JSON 변환 (판정 엔진용) ──
    _json_rules = None
    if getattr(GLOBAL_CONFIG, 'anomaly_nl_autocompile', True):
        try:
            _json_rules = compile_nl_to_json(_ANOMALY_KNOWLEDGE_TEXT, _LLM_FN, cache_dir='RUN/AI')
            if _json_rules:
                print(f"[INFO] NL→JSON 규칙 {len(_json_rules)}개 로드")
        except Exception as _je:
            print(f"[WARN] NL→JSON 변환 실패: {_je}")

    reformatter = pd.read_csv(f'reformatter/{vehicle}_reformatter.csv')

    reformatter_check = reformatter_verify(reformatter)
    if reformatter_check:
        print_status("Reformatter 검증", "ok", f"{vehicle}_reformatter.csv 통과")
    else:
        print_status("Reformatter 검증", "fail", f"{vehicle}_reformatter.csv 실패 → 리포트 미발행")

    if reformatter_check :
        conn = duckdb.connect()

        #test_mode True일 경우 etdata_query 진행하지않고 Report 생성만 진행
        if not test_mode and not trigger_flag:
            # ── ET 데이터 쿼리 (Hive 파티션으로 daily 폴더에 저장) ──
            etdata_query()
            print('[INFO] ==============et_query 수행완료==============')
            # NOTE: et_LOTWF_generator 삭제됨 — daily DB에서 DuckDB로 직접 조회
            log_to_file("Query Success...", query_log)

            wipdata_query()
            print('[INFO] ==============wip_query 수행완료==============')

        et_log = pd.read_csv(et_log_path) # n일 치 et_log
        existing_lot_log = pd.read_csv(Final_et_log_path) if os.path.exists(Final_et_log_path) else pd.DataFrame(columns=['prime_key','wafer_id','step_seq','total_site_cnt',\
                                                                                                                        'tkout_time','lot_id','dc_step_id','dc_done'])

        wip_current = pd.read_csv(DB + f'{vehicle}_wip_current.csv' ,encoding='cp949')
        wip_current['last_update_date'] = pd.to_datetime(wip_current['last_update_date'])
        wip_current = wip_current.sort_values(by='last_update_date')
        grouped = wip_current.groupby('lot_id').last().reset_index()

        # rsplit: vehicle 이름에 언더스코어 포함 가능 대응 (prime_key = mask_fablotid_stepid)
        _pk_parts = et_log['prime_key'].str.rsplit('_', n=2)
        et_log['lot_id'] = _pk_parts.str[1]
        et_log['dc_step_id'] = _pk_parts.str[2]
        et_log = pd.merge(et_log, grouped[['lot_id','step_id']], on='lot_id', how='left')

        combined_lot_log = pd.concat([existing_lot_log, et_log]) 
        final_lot_log = combined_lot_log.drop_duplicates(subset=['prime_key'], keep='last').copy() #기존 et_log update
        final_lot_log['tkout_time'] = pd.to_datetime(final_lot_log['tkout_time'])

        datetime_now_plus = datetime_now - timedelta(minutes=delay_min) 

        # LOT 완료 확인 Logic
        final_lot_log['dc_step_id_num'] = final_lot_log['dc_step_id'].str.extract(r'(\d+)', expand=False).astype(float)
        final_lot_log['step_id_num'] = final_lot_log['step_id'].str.extract(r'(\d+)', expand=False).astype(float)

        final_lot_log['dc_done']= np.where( ((final_lot_log['step_id'].str[:2] != final_lot_log['dc_step_id'].str[:2]) | \
                                            (final_lot_log['step_id'].isnull()) |\
                                            (final_lot_log['step_id_num'] - final_lot_log['dc_step_id_num'] >= 100)) & \
                                            (datetime_now_plus > final_lot_log['tkout_time'] ),True, False)

        # Report 1회만 발송
        # dc_done열에서 True 값을 유지하기 위해 원본 데이터프레임에서 True 값이 있는경우 그대로 반영
        # (행별 combined_lot_log 전체 재필터 O(N^2) apply → prime_key groupby.any() 벡터화)
        _prev_done = combined_lot_log.groupby('prime_key')['dc_done'].any()
        final_lot_log['dc_done'] = (final_lot_log['dc_done'].astype(bool)
                                    | final_lot_log['prime_key'].map(_prev_done).fillna(False).astype(bool))

        final_lot_log.drop('step_id', axis=1, inplace=True)
        final_lot_log.drop('dc_step_id_num', axis=1, inplace=True)
        final_lot_log.drop('step_id_num', axis=1, inplace=True)
        final_lot_log = final_lot_log.sort_values(by='tkout_time', ascending=True)
        final_lot_log.to_csv(Final_et_log_path, index = False)

        selected_et_log = final_lot_log[['lot_id', 'dc_step_id', 'dc_done','tkout_time']]
        selected_et_log_before = existing_lot_log[['lot_id', 'dc_step_id', 'dc_done']]
        selected_et_log_before.rename(columns={'dc_done': 'dc_done_before'}, inplace=True)
        selected_et_log = pd.merge(selected_et_log, selected_et_log_before, on=['lot_id','dc_step_id'], how='left')

        # DC 완료여부 판정 Logic
        dc_done_list = selected_et_log[(selected_et_log['dc_done'] != selected_et_log['dc_done_before'])]
        dc_done_list = dc_done_list[dc_done_list['dc_done'] == True]

        if not trigger_flag:
            print(f"[INFO] {datetime_now} 측정완료 LOT 확인 됨 (총 {len(dc_done_list)}건)")

        if ptype_lot_turnoff == True or ptype_lot_turnoff == 'True' :
            dc_done_list = dc_done_list[~dc_done_list['lot_id'].str.startswith('A4')]
            print(f"[INFO] P-Type(A4*) 제외 후 LOT: {len(dc_done_list)}건")

        if specific_dc_layer is not False:
            dc_done_list['dc_layer_check'] = dc_done_list['dc_step_id'].map(GLOBAL_CONFIG.get("dc_dict"))
            dc_done_list = dc_done_list[dc_done_list['dc_layer_check'] == 'MFDC']
            dc_done_list = dc_done_list.drop(columns=['dc_layer_check'])
            print(f"[INFO] specific_dc_layer 타겟 필터 후 LOT: {len(dc_done_list)}건")

        # trigger_flag = True

        if trigger_flag :
            #trigger
            parts = raw_arg.strip().rsplit("_", 2)
            dc_done_list = {
                'lot_id': [parts[1]],
                'dc_step_id': [parts[2]],
                'dc_done': [True],
                'dc_done_before': [False]
            }

        #수동발행 필요 시 
            # dc_done_list = {
            #     'lot_id': 'A488GA.1',
            #     'dc_step_id': 'CC942300',
            #     'dc_done': [True],
            #     'dc_done_before': [False]
            # }

        if trigger_flag:
            print("[INFO] 강제발행모드입니다. 쿼리 수행되지않고 현재 DB에서 리포팅만 실행합니다.")
        print("[INFO] 리포팅 진행할 LOT LIST")
        dc_done_list = pd.DataFrame(dc_done_list)

        if (not DB_Setting_mode) & (report_making):
            print(f"[INFO] DB_Setting_mode =  {DB_Setting_mode}")
            print(f"[INFO] report_making = {report_making}")
            if not dc_done_list.empty:

                #dc_done_list
                dc_done_list['search_key'] = dc_done_list['lot_id'].astype(str) + '_' + dc_done_list['dc_step_id'].astype(str)
                search_strings = dc_done_list['search_key'].unique().tolist() #측정된 {fab_lot_id}_{dc_step_id} list

                # ================================================================
                # DuckDB: daily Hive 파티션에서 직접 조회 (LOTWF 제거)
                # ================================================================
                DB_et_daily = GLOBAL_CONFIG.get('DB_et_daily')

                # reformatter에서 REAL/ADDP 항목 분리
                item_et = reformatter.copy()
                is_real = item_et['CATEGORY'] == 'REAL'
                is_addp = item_et['CATEGORY'] == 'ADDP'
                real = item_et[is_real][['ITEMID', 'ALIAS', 'SCALE FACTOR', 'ABSOLUTE']].copy()
                addp = item_et[is_addp][['ALIAS', 'ADDP FORM', 'SCALE FACTOR']].copy()
                # SCALE FACTOR 결측/비수치는 1.0으로 (blank이면 'nan*(...)'가 되어 값이 전부 NaN 되는 문제 방지)
                real['SCALE FACTOR'] = pd.to_numeric(real['SCALE FACTOR'], errors='coerce').fillna(1.0)
                addp['SCALE FACTOR'] = pd.to_numeric(addp['SCALE FACTOR'], errors='coerce').fillna(1.0)
                # ADDP FORMULA = (ADDP 자신의 SCALE FACTOR) * (ADDP FORM).
                # ADDP FORM의 {ALIAS}는 이미 SCALE FACTOR가 적용된 REAL/ADDP 컬럼을 참조하므로,
                # '먼저 계산에 들어가는 Alias들이 scale factor 적용된 값'으로 계산된다.
                addp['addpscale'] = addp['SCALE FACTOR'].astype(str) + '*(' + addp['ADDP FORM'].astype(str) + ')'
                ALIAS = list(map(str, addp.ALIAS))
                FORMULA = list(map(str, addp.addpscale))

                # Hive 파티션 glob 패턴
                hive_glob = os.path.join(DB_et_daily, '*', '*.parquet').replace('\\', '/')

                # DuckDB로 viewing_period 범위의 raw 데이터 로드
                raw_query = f"""
                    SELECT *
                    FROM read_parquet('{hive_glob}', hive_partitioning=true)
                    WHERE date >= CURRENT_DATE - INTERVAL '{viewing_period}' DAY
                """
                raw_df = conn.execute(raw_query).df()

                if raw_df.empty:
                    print(f'[WARN] daily DB에 {viewing_period}일 이내 데이터 없음')
                    sys.exit(0)

                # ── Scale Factor 적용 (REAL item 값 × SCALE FACTOR) ──
                # 매칭 안된 raw item은 SCALE FACTOR=1.0 (원값 유지). REAL 값이 여기서 스케일되므로
                # 이후 ADDP(Reformatize) 계산에 들어가는 ALIAS들은 이미 scale factor가 적용된 상태.
                # ITEMID 중복 방지: 같은 ITEMID가 여러 ALIAS로 매핑되면 pivot 시
                # 데이터가 여러 컬럼에 중복 들어감(예: Junction_N+PW_LKG → BV, N+PW).
                # 첫 번째 매칭만 유지하여 1:1 대응 보장.
                _real_dedup = real.drop_duplicates(subset='ITEMID', keep='first')
                raw_df = pd.merge(raw_df, _real_dedup, left_on='item_id', right_on='ITEMID', how='left')
                raw_df['et_value'] = pd.to_numeric(raw_df['et_value'], errors='coerce')
                _sf = pd.to_numeric(raw_df['SCALE FACTOR'], errors='coerce').fillna(1.0)
                raw_df['et_value'] = raw_df['et_value'] * _sf
                raw_df['item_id'] = raw_df['ALIAS'].fillna(raw_df['item_id'])
                raw_df['match_key'] = raw_df['root_lot_id'].astype(str) + '_' + raw_df['step_id'].astype(str)
                raw_df['lot_wf'] = raw_df['root_lot_id'].astype(str) + '_' + raw_df['wafer_id'].astype(str)

                # ── Pivot (세로→가로 전개) ──
                pivot_idx = ['fab_lot_id','lot_id','root_lot_id','wafer_id','process_id','part_id',
                             'step_id','step_seq','tkout_time','flat_zone','eqp_id','probe_card_id',
                             'chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt',
                             'match_key','lot_wf']
                pivot_idx = [c for c in pivot_idx if c in raw_df.columns]

                merged_df = raw_df.pivot_table(
                    values='et_value', index=pivot_idx,
                    columns='item_id', aggfunc='last', observed=True
                )

                # ── ADDP (Index) 계산 ──
                _cols_before_addp = set(merged_df.columns)
                merged_df = Reformatize(merged_df, ALIAS, FORMULA)
                _cols_added_by_addp = set(merged_df.columns) - _cols_before_addp
                merged_df = merged_df.reset_index()
                merged_df['mask'] = vehicle
                # ── with_vehicle 데이터 로드 & Merge (daily Hive 파티션 사용) ──
                if not vehicle in with_vehicle :
                    print("[INFO] with_vehicle안에 vehicle 없음. 진행")
                    try : 
                        with_vehicle_Table = pd.DataFrame() 
                        for with_vehicle_now in with_vehicle :
                            wv_daily_path = DB + with_vehicle_now + '_daily'
                            wv_hive_glob = os.path.join(wv_daily_path, '*', '*.parquet').replace('\\', '/')

                            print(f'[INFO] with_vehicle={with_vehicle_now}, viewing_period={viewing_period}')

                            # daily Hive 파티션에서 with_vehicle 데이터 로드
                            wv_raw_query = f"""
                                SELECT *
                                FROM read_parquet('{wv_hive_glob}', hive_partitioning=true)
                                WHERE date >= CURRENT_DATE - INTERVAL '{viewing_period}' DAY
                            """
                            wv_raw_df = conn.execute(wv_raw_query).df()

                            if wv_raw_df.empty:
                                print(f'[WARN] {with_vehicle_now} daily DB 데이터 없음, 스킵')
                                continue

                            # Scale Factor 적용 (with_vehicle용 reformatter 로드)
                            wv_reformatter = pd.read_csv(f'reformatter/{with_vehicle_now}_reformatter.csv')
                            wv_item = wv_reformatter.copy()
                            wv_real = wv_item[wv_item['CATEGORY'] == 'REAL'][['ITEMID', 'ALIAS', 'SCALE FACTOR', 'ABSOLUTE']].copy()
                            wv_addp = wv_item[wv_item['CATEGORY'] == 'ADDP'][['ALIAS', 'ADDP FORM', 'SCALE FACTOR']].copy()
                            wv_real['SCALE FACTOR'] = pd.to_numeric(wv_real['SCALE FACTOR'], errors='coerce').fillna(1.0)
                            wv_addp['SCALE FACTOR'] = pd.to_numeric(wv_addp['SCALE FACTOR'], errors='coerce').fillna(1.0)
                            wv_addp['addpscale'] = wv_addp['SCALE FACTOR'].astype(str) + '*(' + wv_addp['ADDP FORM'].astype(str) + ')'
                            wv_ALIAS = list(map(str, wv_addp.ALIAS))
                            wv_FORMULA = list(map(str, wv_addp.addpscale))

                            _wv_real_dedup = wv_real.drop_duplicates(subset='ITEMID', keep='first')
                            wv_raw_df = pd.merge(wv_raw_df, _wv_real_dedup, left_on='item_id', right_on='ITEMID', how='left')
                            wv_raw_df['et_value'] = pd.to_numeric(wv_raw_df['et_value'], errors='coerce')
                            wv_raw_df['et_value'] = wv_raw_df['et_value'] * pd.to_numeric(wv_raw_df['SCALE FACTOR'], errors='coerce').fillna(1.0)
                            wv_raw_df['item_id'] = wv_raw_df['ALIAS'].fillna(wv_raw_df['item_id'])
                            wv_raw_df['match_key'] = wv_raw_df['root_lot_id'].astype(str) + '_' + wv_raw_df['step_id'].astype(str)
                            wv_raw_df['lot_wf'] = wv_raw_df['root_lot_id'].astype(str) + '_' + wv_raw_df['wafer_id'].astype(str)

                            wv_pivot_idx = [c for c in pivot_idx if c in wv_raw_df.columns]
                            wv_pivot = wv_raw_df.pivot_table(
                                values='et_value', index=wv_pivot_idx,
                                columns='item_id', aggfunc='last', observed=True
                            )
                            _wv_cols_before = set(wv_pivot.columns)
                            wv_pivot = Reformatize(wv_pivot, wv_ALIAS, wv_FORMULA)
                            _cols_added_by_addp |= (set(wv_pivot.columns) - _wv_cols_before)
                            wv_pivot = wv_pivot.reset_index()
                            wv_pivot['mask'] = with_vehicle_now

                            with_vehicle_Table = pd.concat([with_vehicle_Table, wv_pivot], ignore_index=True)

                    except :
                        with_vehicle_Table = pd.DataFrame()

                    merged_df = pd.concat([merged_df,with_vehicle_Table], ignore_index=True)
                    if vehicle in ["Solomon1", "Solomon2"]:
                        merged_df.to_parquet(f"ET_TABLE_Solomon.parquet")

                df_include_column = reformatter[['ALIAS','REPORT ORDER']].dropna(subset=['REPORT ORDER']).drop('REPORT ORDER',axis=1)
                columns_to_include_1 = df_include_column['ALIAS'].tolist()
                columns_to_include_2 = ['fab_lot_id','lot_id','mask','lot_wf','root_lot_id','wafer_id','process_id','part_id','step_id','step_seq'\
                                            ,'tkout_time','flat_zone','eqp_id','probe_card_id','chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt','match_key']
                # PCHK(Probe check) 컬럼은 차트/스코어보드엔 안 쓰지만, 측정 신뢰성 분석(동일 site 이탈)
                # 을 위해 merged_df에 유지한다. (REPORT ORDER가 없어 include_1엔 안 잡히므로 별도 보존)
                # 인식 기준: ALIAS에 'PCHK' 포함 또는 reformatter CAT2가 'PCHK'
                # (alias에 PCHK가 없어도 CAT2로 지정하면 유지 — anomaly_engine 인식 기준과 동일)
                _pchk_cat2_aliases = set()
                if 'CAT2' in reformatter.columns:
                    _pchk_cat2_aliases = {str(a) for a in reformatter.loc[
                        reformatter['CAT2'].astype(str).str.upper() == 'PCHK', 'ALIAS'].dropna()}
                pchk_keep = [c for c in merged_df.columns
                             if 'PCHK' in str(c).upper() or str(c) in _pchk_cat2_aliases]
                # 다중컬럼 ADDP 파생(MA_Window 등: {alias}_minus_margin/_ovl_index 등)도 유지.
                # Reformatize가 실제로 추가한 컬럼만 유지 — startswith 방식은 ALIAS 접두어가
                # 겹치는 별개 아이템(예: LKG → LKG_REMOVE_L)을 잘못 포함하는 문제가 있었음.
                derived_addp = [c for c in _cols_added_by_addp
                                if c not in columns_to_include_1]
                columns_to_include = columns_to_include_1 + columns_to_include_2 + pchk_keep + derived_addp
                filtered_columns = [col for col in columns_to_include if col in merged_df.columns]
                merged_df = merged_df[list(dict.fromkeys(filtered_columns))]

                columns_to_exclude_1 = [col for col in merged_df.columns if 'PCHK' in col]
                columns_to_exclude_2 = ['fab_lot_id','lot_id','mask','lot_wf','root_lot_id','wafer_id','process_id','part_id','step_id','step_seq'\
                                        ,'tkout_time','flat_zone','eqp_id','probe_card_id','chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt']
                columns_to_exclude = columns_to_exclude_1 +  columns_to_exclude_2
                columns_to_check = merged_df.columns.difference(columns_to_exclude)
                merged_df = merged_df.dropna(subset=columns_to_check, how='all')

                merged_df['wafer_id'] = merged_df['wafer_id'].astype(int)
                merged_df['DC_Split'] = merged_df['step_id'].replace(GLOBAL_CONFIG.get("dc_dict"))
                merged_df['search_key'] = merged_df['fab_lot_id'].astype(str) + "_" + merged_df['step_id'].astype(str)
                merged_df['match_key'] = merged_df['root_lot_id'].astype(str) + "_" + merged_df['step_id'].astype(str)
                merged_df['tkout_time'] = pd.to_datetime(merged_df['tkout_time'])

                # Change data type
                merged_df = merged_df.astype({'wafer_id': int, 'chip_x_pos': int, 'chip_y_pos': int, 'flat_zone': int, 'temperature': float})

                # Add TEMPERATURE Modified
                merged_df['temperature'] = merged_df['temperature'].apply(lambda a: int(np.round(a / 5) * 5))
                # =====================================================================================================
                # merged_df = merged_df[merged_df['step_seq'] == 'N02V98HI']

                # Add coordinate_file
                coordinate_file = pd.read_excel(coordinate_file_path, sheet_name=None, engine='openpyxl')
                zone_define = coordinate_file['Zone_Define']
                zone_define['MASK'] = zone_define['MASK'].replace('RHV_OS','RHV-OS') #RHV OS Vehicle 명 상이함. matching을 위한 변경
                zone_define = zone_define.astype({'CHIP_X_POS': int, 'CHIP_Y_POS': int, 'CHIP_X_ADJ': int, 'CHIP_Y_ADJ': int, 'FLAT_ZONE_POS': int})
                # WF MAP geometry(150mm 원 fit·shot pitch)를 측정 데이터가 아닌 좌표파일의
                # MASK(vehicle)별 전체 chip layout(CHIP_X_ADJ/CHIP_Y_ADJ/Chip_Radius) 기준으로
                # 계산하도록 등록 — 측정 pt가 적은(예 13pt) wafer도 WF MAP이 깨지지 않는다.
                set_chip_layout(zone_define)

                # =====================================================================================================

                # Add Point column
                merged_df['Point'] = 1
                merged_df['Point'] = merged_df.groupby(['fab_lot_id','wafer_id','tkout_time'], observed=False)['Point'].transform('sum').astype(str) # # ,'STEP_ID', 'STEP_SEQ'

                # Add duplicate count
                merged_df['Duplicate_Count'] = merged_df.groupby(['DC_Split','temperature','flat_zone','fab_lot_id','wafer_id','step_seq','Point'], observed=False)['tkout_time'].rank(method='dense')

                # Add PGM(pt)_CNT
                merged_df['PGM(pt)'] = list(map(lambda a,b,c: f"{a}({b}pt)_{c}", merged_df['step_seq'],merged_df['Point'],merged_df['Duplicate_Count']))

                # column명 통일
                new_column_names = []
                ref_column_names = ['fab_lot_id',
                                    'lot_wf',
                                    'lot_id',
                                    'mask',
                                    'root_lot_id', 
                                    'wafer_id', 
                                    'process_id', 
                                    'part_id', 
                                    'tkout_time', 
                                    'temperature', 
                                    'item_id', 
                                    'flat_zone', 
                                    'chip_x_pos', 
                                    'chip_y_pos',
                                    'subitem_id', 
                                    'et_value',
                                    'step_id', 
                                    'step_seq', 
                                    'eqp_id', 
                                    'probe_card_id',
                                    'point',
                                    'total_site_cnt']

                for col in merged_df.columns:
                    if col in ref_column_names:
                        if 'flat_zone' in col: 
                            new_column_names.append('FLAT_ZONE_POS')
                        else:
                            new_column_names.append(col.upper())
                    else:
                        new_column_names.append(col)

                # Set new column names
                merged_df.columns = new_column_names

                # Zone Radius add
                merged_df = pd.merge(merged_df,zone_define,on=['MASK','CHIP_X_POS','CHIP_Y_POS','FLAT_ZONE_POS'])

                html_code = GLOBAL_CONFIG.get("html_code")

                # Description PPT 파싱은 lot과 무관(경로/품질만 의존) → 랏 루프 밖에서 1회만 수행
                description_image_info_dict_low_qual = calcaulate_description_image_info_dict(description_ppt_path, img_quality = 20)

                for search_key in search_strings : #search key = match key, fablot_id + dc_step_id
                    try :
                        _t_report_start = time.perf_counter()
                        print_status("Report 발행 시작", "info", f"{search_key}")

                        target_lot_id = search_key.split('_')[0] #{fab_lot_id}
                        target_root_lot_id = target_lot_id[:5] #{root_lot_id}
                        target_DC_step_id = search_key.split('_')[1] #{DC_step_id}
                        target_DC_step = GLOBAL_CONFIG.get("dc_dict").get(target_DC_step_id) #{DC_step}
                        target_step_merged = target_DC_step + "(" + target_DC_step_id + ")" #{DC_step_id}({DC_step})

                        match_key = target_root_lot_id + "_" + target_DC_step_id #match_key = {root_lot_id}_{DC_step_id}
                        # 리포트 키 = {fab_lot_id}_{step_id}(원본 키) — anomaly_basis/ai_input/
                        # rule_check/ARCHIVE 산출물 파일명이 전부 이 키를 공유(step별 덮어쓰기 방지)
                        report_key = f"{target_lot_id}_{target_DC_step_id}"

                        # print('***** fab_lot_id + step_id : ', search_key)
                        # print('***** root_lot_id + step_id : ', match_key)

                        df = merged_df[merged_df['match_key'] == match_key].copy()

                        search_key_rows = df[df['search_key'] == search_key]

                        # 이 step_id에 target lot의 실제 측정 행이 있어야만 발행한다.
                        # search_key = fab_lot_id + step_id. lot_id만 match_key(root+step)로
                        # 잡히고(형제 lot이 이 step을 측정) 정작 target lot 자신은 이 step에
                        # 측정 데이터가 없으면 — lot_id만 매칭된 것이므로 — 리포트/메일을 만들지 않는다.
                        # (empty_cols→df.drop 경유 df.empty로도 걸러지지만, 여기서 명시적으로
                        #  '측정 데이터 없음'을 정확한 메시지로 조기 skip한다.)
                        if search_key_rows.empty:
                            print(f"{search_key}에 해당 step 측정 데이터가 없어 Report가 발행되지 않았습니다.")
                            log_to_file(f"{search_key}에서 해당 step_id에 측정 데이터가 없어(lot_id만 매칭) Report 발행되지 않았습니다.", error_log)
                            continue

                        df['WAFER_ID'] = df['WAFER_ID'].astype(int)
                        empty_cols = search_key_rows.columns[search_key_rows.isna().all()]

                        df.drop(columns=empty_cols, inplace=True)

                        if df.empty :
                            print(f"{search_key}가 비어있습니다.")
                            log_to_file(f"{search_key}에서 HOL DATA가 측정되지 않아 Report 발행되지 않았습니다.", error_log)
                            continue

                        # 이 step_id에서 측정된 게 PCHK(측정 신뢰성) 항목뿐이고 실제 device
                        # 측정 item이 하나도 없으면 "측정 안 됨"으로 간주 → mail/report 미발행.
                        #  · device item = REPORT ORDER 보유 ALIAS(columns_to_include_1) + ADDP 파생
                        #    (derived_addp) 중 PCHK 계열(pchk_keep / 이름에 'PCHK')을 제외한 컬럼.
                        #  · 판정 기준 = target lot의 이 step 행(search_key_rows)에 실측값 존재 여부.
                        #    (empty_cols 판정과 동일 스코프 → 리포트 내용 항목과 일치)
                        _pchk_col_set = set(pchk_keep)
                        _device_item_cols = [c for c in (columns_to_include_1 + derived_addp)
                                             if c in search_key_rows.columns
                                             and c not in _pchk_col_set
                                             and 'PCHK' not in str(c).upper()]
                        if not any(search_key_rows[c].notna().any() for c in _device_item_cols):
                            print(f"{search_key}에서 PCHK 항목만 측정되어 Report가 발행되지 않았습니다.")
                            log_to_file(f"{search_key}에서 device 측정 item 없이 PCHK 항목만 측정되어 Report 발행되지 않았습니다.", error_log)
                            continue


                        target_wafer_id_list = sorted(df['WAFER_ID'].unique().tolist())
                        print(f'[INFO] 대상 Wafer 목록: {target_wafer_id_list}')

                        #Inline Data 추출
                        print(f'{target_root_lot_id} inline data 추출 시작!')
                        inlinedata = inlinedata_query(target_root_lot_id)
                        print(f'{target_root_lot_id} inline data 추출 완료!')

                        # =====================================================================================================

                        spec_data = reformatter[(~reformatter['REPORT ORDER'].isnull())] #Report order가 존재하는 item만 spec data확인
                        spec_dict = {row['ALIAS']: (row['SPECLOW'], row['SPECHIGH']) for _, row in spec_data.iterrows()} #dict형식으로 빠른 접근가능
                        # REPORT DIRECTION: UPPER=상한만, LOWER=하한만, BOTH=둘 다 (합격판정에 반영)
                        spec_dir = {}
                        for _, _r in spec_data.iterrows():
                            _d = str(_r['REPORT DIRECTION']).strip().upper() if 'REPORT DIRECTION' in spec_data.columns and pd.notna(_r.get('REPORT DIRECTION')) else 'BOTH'
                            spec_dir[_r['ALIAS']] = _d if _d in ('UPPER', 'LOWER', 'BOTH') else 'BOTH'
                        spec_data = spec_data.set_index('ALIAS')

                        # ========================================= Pass_Rate(Score) 계산 ========================================
                        reformatter['pass_rate'] = 'pass_rate_' + reformatter['ALIAS'] 
                        reformatter = reformatter.set_index('pass_rate')

                        # 각 아이템에 대해 pass_rate_Item{num} *report order가 있는 item한
                        pass_df = pd.DataFrame()
                        for item in spec_dict:
                            try :
                                _low = float(spec_dict[item][0]); _high = float(spec_dict[item][1])
                                _dir = spec_dir.get(item, 'BOTH')
                                def _passfn(x, low=_low, high=_high, direction=_dir):
                                    if pd.isna(x): return x
                                    x = float(x)
                                    if direction == 'UPPER': return 1 if x <= high else 0   # 상한만
                                    if direction == 'LOWER': return 1 if x >= low else 0    # 하한만
                                    return 1 if (x >= low and x <= high) else 0             # BOTH
                                pass_df[f'{item}'] = df[item].astype(float).apply(_passfn)
                            except KeyError:
                                print(f"Pass Rate 계산 Error 발생: '{item}' - Column not found in dataframe")
                            except (ValueError, TypeError):
                                # Check if SPEC values are invalid
                                if item in spec_dict and (spec_dict[item][0] is None or spec_dict[item][1] is None):
                                    print(f"Pass Rate 계산 Error 발생: '{item}' - Invalid SPEC values (None/NaN)")
                                else:
                                    print(f"Pass Rate 계산 Error 발생: '{item}' - Non-numeric data in column")
                            except Exception as e:
                                print(f"Pass Rate 계산 Error 발생: '{item}' - {str(e)}")
                        pass_df.columns = 'pass_rate_' + pass_df.columns 

                        df = pd.concat([df, pass_df], axis=1)
                        # ============================================ VIP_group 생성 ===========================================

                        # match_key와 맞는 data filtering
                        wf_matching_list = list(zip(df['FAB_LOT_ID'], df['WAFER_ID'].astype(str).apply(lambda x: '#' + x)))
                        wf_matching_list = list(set(wf_matching_list))
                        # print('wf_matching_list : ',wf_matching_list)

                        # VIP_group_raw 생성
                        selected_columns = ['WAFER_ID'] + [col for col in df.columns if col.startswith('pass_rate_')]
                        pivot_group = df[selected_columns]
                        pivot_group = pivot_group.groupby('WAFER_ID').mean()*100
                        pivot_group = pivot_group.T
                        VIP_group_raw = pd.merge(pivot_group, reformatter[['REPORT ORDER','PPT_ONLY']], right_index=True, left_index=True, how='right').sort_values('REPORT ORDER').dropna(subset=['REPORT ORDER'])
                        # RIGHT join으로 reformatter 전체 항목이 들어오므로, 실제 pass_rate 컬럼이
                        # df에 존재하는(=측정 데이터가 있는) 항목만 유지 — 미측정 항목 제거
                        _existing_pr = {c for c in df.columns if c.startswith('pass_rate_')}
                        VIP_group_raw = VIP_group_raw[VIP_group_raw.index.isin(_existing_pr)]
                        VIP_group = VIP_group_raw.drop(['REPORT ORDER', 'PPT_ONLY'], axis=1, errors='ignore').dropna(how='all')
                        # PPT_ONLY=True 항목은 HTML score board에서 제외(PPT에만 표시).
                        # 값이 bool/1.0/"True"/"1"/"Y" 등 어떤 형태여도 truthy로 인식하도록 처리.
                        def _ppt_only_true(v):
                            if pd.isna(v):
                                return False
                            if isinstance(v, str):
                                return v.strip().lower() in ('true', '1', '1.0', 'y', 'yes', 't')
                            try:
                                return float(v) == 1.0
                            except (TypeError, ValueError):
                                return bool(v)
                        _ppt_mask = VIP_group_raw['PPT_ONLY'].map(_ppt_only_true)
                        VIP_group_raw = VIP_group_raw[~_ppt_mask]   # HTML용: PPT_ONLY 제외
                        VIP_group_raw = VIP_group_raw.drop('PPT_ONLY', axis=1)

                        # VIP_group 생성 *presentation 생성용 dataframe
                        VIP_group.index = VIP_group.index.str.replace('pass_rate_', '')

                        # PPT Score Board용 (lot, wafer) 분리 pivot — VIP_group과 같은 행순서, 컬럼만 lot별 분리
                        _sb_pass = [c for c in df.columns if c.startswith('pass_rate_')]
                        _sb_lw = (df[['FAB_LOT_ID', 'WAFER_ID'] + _sb_pass]
                                  .groupby(['FAB_LOT_ID', 'WAFER_ID']).mean() * 100).T
                        _sb_lw.index = _sb_lw.index.str.replace('pass_rate_', '')
                        _sb_lw.columns = pd.MultiIndex.from_tuples(
                            [(str(_l), int(float(_w))) for (_l, _w) in _sb_lw.columns])
                        VIP_group_lw = _sb_lw.reindex(VIP_group.index)   # 행=VIP_group 순서, 컬럼=(lot,wafer)

                        # VIP_group_HTML 생성 *VIP_group copy (HTML 카테고리 구분자는 CAT2 기준)
                        VIP_group_HTML = pd.merge(VIP_group_raw,reformatter[['CAT2','REPORT ORDER']].dropna(subset=['REPORT ORDER']).drop('REPORT ORDER',axis=1)\
                                                ,right_index=True, left_index=True, how='left').reset_index()
                        VIP_group_HTML = VIP_group_HTML.rename(columns={'CAT2': 'CATEGORY', 'index': 'ITEM_ID', 'pass_rate': 'ITEM_ID'})
                        VIP_group_HTML['ITEM_ID'] = VIP_group_HTML['ITEM_ID'].str.replace('pass_rate_', '')
                        VIP_group_HTML = VIP_group_HTML.set_index(['CATEGORY', 'ITEM_ID'])
                        VIP_group_HTML = VIP_group_HTML.drop('REPORT ORDER',axis=1)
                        VIP_group_HTML = VIP_group_HTML.dropna(how='all')

                        # ========================================= PPT file name 생성 ==========================================

                        rname = f'HOL_{target_DC_step}_Report'
                        fname = f'{upload_date}-{prod}-{target_root_lot_id}-{rname}.html' #html 저장이름
                        final_ppt_file_name_DX = f'{upload_date}-{prod}-{target_root_lot_id}-{rname}.pptx' #pptx 저장이름, DX System 및 S3 DB 저장

                        # ========================================= 저화질 버전 ppt 제작 =========================================

                        clear_temp_inside_run()
                        clear_anomaly_inside_run()

                        # 1-1. Title page 투입
                        print(f'[INFO]..{vehicle}_{target_lot_id}_{target_step_merged}_HOL_AUTO_REPORT 저화질 버전 제작 시작..\n')
                        prs_low_qual = make_title_page(vehicle, target_lot_id, target_step_merged)

                        # 1-2. Scoreboard 투입 (lot_id 분리 — HTML과 동일하게 (lot,wafer) 컬럼)
                        _sb_item_cells = {}   # Score Board Item명 셀 → 차트 슬라이드 링크용(insert_plots 후 연결)
                        prs_low_qual = insert_score_board(VIP_group_lw, prs_low_qual, target_lot_id, ' / '.join([target_lot_id, target_step_merged]), spec_data=spec_data, config=GLOBAL_CONFIG, item_link_cells=_sb_item_cells)

                        # 1-3. BoxPlot 투입 - 메일링 버전 (description dict는 랏 루프 밖에서 1회 파싱)
                        prs_low_qual, metrics_dict, item_slide_map = insert_plots(merged_df, prs_low_qual, description_image_info_dict_low_qual, target_lot_id, target_root_lot_id, target_DC_step, target_DC_step_id, spec_data, img_quality = 12, ref=False, reformatter=reformatter, dpi=GLOBAL_CONFIG.ppt_chart_dpi)

                        # Score Board Item명 → 해당 차트 슬라이드 내부 하이퍼링크 연결(차트 슬라이드 생성 후)
                        link_scoreboard_items(_sb_item_cells, item_slide_map)

                        # 1-3b. 코드 통계 분석(findings) — HTML [0]와 PPT 상세 페이지에 공용 사용
                        #   ⚠️ 지식판정(RULE) 기능은 AI 연결 시에만 동작 — AI 미연결이면 기존 이상/주의 판정만.
                        _ai_on = bool(GLOBAL_CONFIG.use_gpt_summary
                                      and getattr(GLOBAL_CONFIG, 'use_gpt_multistep', True)
                                      and _LLM_FN is not None)
                        code_findings = []
                        anomaly_item_stats = {}   # 항목별 통계 요약 — AI 해석 [항목 통계] 입력
                        anomaly_rule_trace = []   # 전체 anomaly rule 체크 결과(매칭/해당없음) — RUN/AI 저장·PPT 반영
                        # anomaly 분석 입력을 '현재 step_id'로 한정 — 다른 step에서 측정된 항목의
                        # 이상이 이 리포트(키=lot+step)의 finding/Anomaly 차트에 섞이지 않게 한다.
                        #  (insert_plots는 이미 match_key(root+step)로 step을 스코프 → metrics_dict·
                        #   Trend PNG는 step 한정. analyze_commonality만 lot 단위라 cross-step 이상이
                        #   섞이던 문제. STEP_ID 값 == target_DC_step_id 는 search_key 매칭으로 보장.)
                        _anom_df = merged_df
                        _step_col = next((c for c in ('STEP_ID', 'step_id') if c in merged_df.columns), None)
                        if _step_col is not None:
                            _sd = merged_df[merged_df[_step_col].astype(str) == str(target_DC_step_id)]
                            if not _sd.empty:
                                _anom_df = _sd
                        try:
                            code_findings = analyze_commonality(
                                _anom_df, target_lot_id, metrics_dict, spec_data,
                                main_vehicle=vehicle, config=GLOBAL_CONFIG, reformatter=reformatter,
                                knowledge_text=_ANOMALY_KNOWLEDGE_TEXT,
                                item_stats_out=anomaly_item_stats,
                                rule_trace_out=anomaly_rule_trace,
                                json_rules=(_json_rules if _ai_on else None),
                                report_key=report_key)
                            print(f"[INFO] commonality 분석: {len(code_findings)}건 finding")
                        except Exception as ce:
                            print(f"[WARN] commonality 분석 스킵 (오류): {ce}")
                        # 전체 anomaly rule 체크 결과를 RUN/AI 폴더에 파일로 저장(매칭·해당없음 전량 기록)
                        try:
                            _save_rule_check_log(_ai_dir, target_lot_id, target_DC_step_id,
                                                 anomaly_rule_trace, code_findings)
                        except Exception as _rce:
                            print(f"[WARN] rule 체크 결과 저장 스킵 (오류): {_rce}")
                        # 발행 스냅샷(RUN/ARCHIVE/<key>/) — 규칙 제안 다이제스트·사례 아카이브 입력.
                        #   부가 산출물: 지워지거나 없어도 리포트 발행/판정에 영향 없음(저장 실패도 무시).
                        if getattr(GLOBAL_CONFIG, 'use_archive_snapshot', True):
                            try:
                                _save_archive_snapshot(
                                    report_key,
                                    {'report_key': report_key, 'target_lot_id': target_lot_id,
                                     'step_id': target_DC_step_id, 'dc_step': target_DC_step,
                                     'vehicle': vehicle, 'wafers': target_wafer_id_list,
                                     'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                                    code_findings, anomaly_item_stats, anomaly_rule_trace,
                                    target_rows=search_key_rows,
                                    # 당시 index = REPORT ORDER 보유 항목 그대로(사내 reformatter는
                                    # PCHK_LKG/PCHK_RES에도 REPORT ORDER가 있어 자연히 포함됨)
                                    index_items=list(spec_data.index))
                            except Exception as _ase:
                                print(f"[WARN] 발행 스냅샷 저장 스킵 (오류): {_ase}")
                        # Score Board 바로 뒤에 'Anomaly 상세(통계)' 페이지 삽입
                        try:
                            _sb_pages = (len(VIP_group) - 1) // 30 + 1
                            prs_low_qual = insert_findings_page(
                                prs_low_qual, code_findings, after_index=1 + _sb_pages,
                                main_vehicle=vehicle,
                                radius_zones=GLOBAL_CONFIG.get('radius_zones', [60, 100]),
                                item_slide_map=item_slide_map,
                                rule_trace=anomaly_rule_trace)
                        except Exception as fe:
                            print(f"[WARN] Anomaly 상세 페이지 삽입 스킵: {fe}")

                        # Score Board → 통계표(Index Aggregation Table) 순서로 인접 배치
                        _move_aggregation_after_scoreboard(prs_low_qual)

                        # 1-4. Save ppt - 메일링 버전
                        if not os.path.exists(low_qual_ppt_save_path):
                            os.makedirs(low_qual_ppt_save_path)
                        try:
                            prs_low_qual.save(f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}')
                            print('[INFO]..저장 완료..\n')
                        except PermissionError:
                            print(f"[WARN] PermissionError: PPT 파일을 저장할 수 없습니다 (파일이 열려있을 수 있습니다): {final_ppt_file_name_DX}")

                        # =====================================================================================================
                        VIP_group = VIP_group.map(lambda x: x.strip() if isinstance(x, str) else x)
                        VIP_group = VIP_group.apply(pd.to_numeric, errors='coerce')
                        VIP_group = VIP_group.dropna(axis=1, how='all')
                        VIP_group = VIP_group.astype(float)
                        VIP_group = VIP_group.round(1) # score table

                        et_log = pd.read_csv(Final_et_log_path)
                        et_log = et_log[['prime_key','wafer_id','step_seq','tkout_time','dc_step_id','dc_done']]
                        et_log = et_log.sort_values(by='tkout_time', ascending=False)
                        et_log = et_log.iloc[:et_log_show,:] #아래에서 n개행만 출력 

                        et_log['LOT ID'] = et_log['prime_key'].str.rsplit('_', n=2).str[1]
                        et_log['WAFER ID'] = et_log['wafer_id'].apply(extract_and_sort_numbers)
                        et_log['DC STEP'] = et_log['dc_step_id'].replace(GLOBAL_CONFIG.get("dc_dict"))
                        et_log['DC 측정완료 여부'] = et_log['dc_done'].apply(lambda x : "RUN 중" if x == False else "측정완료")
                        et_log['측정된 DCOP List'] = et_log['step_seq'].apply(remove_brackets)
                        et_log['DC 측정완료 시간'] = et_log['tkout_time']

                        et_log = et_log[['LOT ID','WAFER ID','DC STEP','DC 측정완료 여부','측정된 DCOP List','DC 측정완료 시간']]

                        inlinedata['ITEMNAME'] = inlinedata['ITEMNAME'].astype(str)
                        inlinedata['item_id'] = inlinedata['item_id'].astype(str)
                        inlinedata['STEP_DESC_ITEM_ID'] = inlinedata['ITEMNAME'] + "_" + inlinedata['item_id']

                        inlinedata_spec = inlinedata.groupby('STEP_DESC_ITEM_ID')[['spc_ctrl_spec_high', 'spc_ctrl_spec_limit', 'spc_ctrl_spec_low']].mean()

                        inlinedata_spec.rename(columns={'spc_ctrl_spec_high': 'UCL'}, inplace=True)
                        inlinedata_spec.rename(columns={'spc_ctrl_spec_limit': 'CL'}, inplace=True)
                        inlinedata_spec.rename(columns={'spc_ctrl_spec_low': 'LCL'}, inplace=True)

                        #spec이 음수인 경우 0으로 변환
                        cols_to_replace = ['UCL', 'CL', 'LCL']
                        for col in cols_to_replace:
                            inlinedata_spec[col] = inlinedata_spec[col].apply(replace_negatives_with_0)

                        inlinedata['fab_value'] = inlinedata['fab_value'].astype(float)
                        inlinedata['tkout_time'] = pd.to_datetime(inlinedata['tkout_time'], format='%Y-%m-%d %H:%M:%S')
                        inlinedata = inlinedata.sort_values(by='tkout_time', ascending=True)

                        inlinedata_pivot = inlinedata.pivot_table(values='fab_value',\
                                                                    index='wafer_id',\
                                                                    columns='STEP_DESC_ITEM_ID', aggfunc='mean',observed = True)

                        # 데이터프레임에 있는 열만 선택하여 새로운 리스트 생성
                        Inline_setting_file = pd.read_excel(inline_file_path, sheet_name=None, engine='openpyxl')
                        Inline1 = Inline_setting_file[inline_file_sheet]
                        inline_filtered = Inline1[Inline1['Key'] == True] 
                        inline_filtered['STEP_DESC_ITEM_ID'] = inline_filtered['ITEMNAME'] + '_' + inline_filtered['ITEM_ID'] 
                        inline_grouped  = inline_filtered.groupby('STEP_DESC_ITEM_ID')['Module'].last()
                        inline_grouped = inline_grouped.reset_index()
                        inline_grouped_dict = inline_grouped.set_index('STEP_DESC_ITEM_ID')['Module'].to_dict() #Inline ITEM과 Module Matching된 dict
                        inline_grouped_dict_ITEMNAME = inline_filtered.set_index('STEP_DESC_ITEM_ID')['ITEMNAME'].to_dict() #Inline ITEM과 ITEMNAME Matching된 dict
                        inline_grouped_dict_ITEM_ID = inline_filtered.set_index('STEP_DESC_ITEM_ID')['ITEM_ID'].to_dict() #Inline ITEM과 ITEM_ID Matching된 dict
                        # STEP_DESC 열이 있으면 'Step desc' 컬럼 소스로 사용(없으면 ITEMNAME fallback)
                        if 'STEP_DESC' in inline_filtered.columns:
                            inline_grouped_dict_STEP_DESC = inline_filtered.set_index('STEP_DESC_ITEM_ID')['STEP_DESC'].to_dict()
                        else:
                            inline_grouped_dict_STEP_DESC = inline_grouped_dict_ITEMNAME
                        inline_filtered_columns = sorted(inline_grouped['STEP_DESC_ITEM_ID'].unique().tolist(), key=lambda s: float(s.split()[0]))

                        valid_columns = [col for col in inline_filtered_columns if col in inlinedata_pivot.columns]
                        inlinedata_filtered = inlinedata_pivot[valid_columns]



                        inlinedata_filtered_pivot = inlinedata_filtered.transpose()

                        # 모든 컬럼명을 정수로 변경하기 위한 딕셔너리 생성
                        #column_map = {old_col: int(old_col) for old_col in inlinedata_filtered_pivot.columns}
                        column_map = {old_col: int(old_col) for old_col in inlinedata_filtered_pivot.columns if old_col.isdigit()}
                        inlinedata_filtered_pivot = inlinedata_filtered_pivot.rename(columns=column_map)

                        #sorted_columns = sorted(inlinedata_filtered_pivot.columns, key=lambda x: int(x))
                        #sorted_columns = sorted([col for col in inlinedata_filtered_pivot.columns if col.isdigit()], key=int)
                        sorted_columns = sorted([col for col in inlinedata_filtered_pivot.columns if str(col).isdigit()], key=lambda x: int(x))

                        inlinedata_filtered_pivot = inlinedata_filtered_pivot[sorted_columns]

                        inlinedata_filtered_pivot = pd.merge(inlinedata_spec, inlinedata_filtered_pivot,how='right', on='STEP_DESC_ITEM_ID')

                        # [PATCH] Inline Table 멀티 인덱스 (UCL 앞 4열: Module / Step desc / ITEMNAME / Item)
                        #  - Module    : inline setting의 실제 Module 열 (inline_grouped_dict)  → 첫번째 인덱스
                        #  - Step desc : STEP_DESC 열 (inline_grouped_dict_STEP_DESC)
                        #  - ITEMNAME  : inline setting의 ITEMNAME 열 (inline_grouped_dict_ITEMNAME)
                        #  - Item      : ITEM_ID 열
                        inlinedata_filtered_pivot['Module'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict)
                        inlinedata_filtered_pivot['Step_desc'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict_STEP_DESC)
                        inlinedata_filtered_pivot['ITEMNAME'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict_ITEMNAME)
                        inlinedata_filtered_pivot['ITEM_ID'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict_ITEM_ID)
                        inlinedata_filtered_pivot = inlinedata_filtered_pivot.set_index(['Module', 'Step_desc', 'ITEMNAME', 'ITEM_ID'])
                        inlinedata_filtered_pivot.index.names = ['Module', 'Step desc', 'ITEMNAME', 'Item']


                        # HTML 생성부분 - Mail body
                        # ===== Score Board 컬럼을 (FAB_LOT_ID, WAFER_ID)로 구성 =====
                        # 같은 root_lot_id의 형제 lot을 wafer 평균으로 합치지 않고 lot별로 분리 표시.
                        _pass_cols = [c for c in df.columns if c.startswith('pass_rate_')]
                        _pivot_lw = (df[['FAB_LOT_ID', 'WAFER_ID'] + _pass_cols]
                                     .groupby(['FAB_LOT_ID', 'WAFER_ID']).mean() * 100).T
                        _pivot_lw.index = _pivot_lw.index.str.replace('pass_rate_', '')

                        def _waf_int(w):
                            # wafer level 정규화 (#1.0 방지 + WF MAP 키 정합)
                            try:
                                return int(float(w))
                            except (ValueError, TypeError):
                                return w
                        _pivot_lw.columns = pd.MultiIndex.from_tuples(
                            [(str(l), _waf_int(w)) for (l, w) in _pivot_lw.columns])

                        # VIP_group_HTML의 (CATEGORY, ITEM_ID) 행 순서/카테고리는 유지하고 데이터만 교체
                        _items_order = list(VIP_group_HTML.index.get_level_values('ITEM_ID'))
                        _data_lw = _pivot_lw.reindex(_items_order)
                        _data_lw.index = VIP_group_HTML.index
                        VIP_group_HTML = _data_lw

                        # 컬럼 정렬: target lot(해당 report lot_id)을 맨 왼쪽, 그 외 형제 lot(이름순) / lot 내 wafer 오름차순
                        _all_cols = list(VIP_group_HTML.columns)
                        _lots = list(dict.fromkeys([c[0] for c in _all_cols]))

                        def _lot_rank(l):
                            if str(l) == str(target_lot_id):
                                return (0, str(l))
                            return (1, str(l))
                        _lots_sorted = sorted(_lots, key=_lot_rank)
                        _ordered_cols = []
                        for _lot in _lots_sorted:
                            _wafs = sorted([c for c in _all_cols if c[0] == _lot],
                                           key=lambda c: (c[1] if isinstance(c[1], int) else 10 ** 9))
                            _ordered_cols.extend(_wafs)
                        VIP_group_HTML = VIP_group_HTML[_ordered_cols]
                        VIP_group_HTML.index.names = ['category', 'Item']
                        print("score board lots :", _lots_sorted)

                        # 측정값이 전혀 없는 행 제거 — PPT와 동일하게 lot-wafer reindex 후에도
                        # 첫 번째 dropna(VIP_group_HTML 초기 생성 시)를 통과한 항목은 유지.
                        # _existing_pr 필터(VIP_group_raw 생성 직후)로 미측정 항목은 이미 제거됨.
                        # 여기서 다시 dropna 하면 lot-wafer 분리 시 일부 lot에만 데이터가 있는
                        # 항목이 잘못 제거되어 "HTML에 2개만 표시"되는 버그 발생.
                        # (NaN 셀은 HTML 렌더러가 회색으로 표기 — line 1440 참조)
                        # VIP_group_HTML = VIP_group_HTML.dropna(how='all')  # 제거: PPT와 일관성 유지

                        # ==================== Score Board HTML 렌더링 (Manual) ====================
                        # Pandas의 to_html()이 만드는 불안정한 멀티인덱스 태그를 방지하기 위해 HTML 태그를 한 땀 한 땀 생성
                        # - 좌측 고정열(LOT_ID/category/Item)은 클래스 기반 sticky (rowspan 사용해도 안깨짐)
                        # - category(CAT2) 연속 동일값은 rowspan으로 병합
                        sb_rows = list(VIP_group_HTML.iterrows())
                        _wcols = list(VIP_group_HTML.columns)

                        # Score Board WF MAP은 용량 문제로 제거됨 — WF MAP은 PPT에서만 확인.
                        # (렌더링/합성 코드와 scoreboard_wfmap_min_pts 설정도 함께 삭제)

                        # 렌더 시퀀스: index 점수행만.
                        render_seq = []   # (kind, cat, item, payload)
                        for idx, row in sb_rows:
                            cat, item = idx
                            render_seq.append(('score', cat, item, row))

                        # category 연속 묶음 rowspan
                        seq_cats = [r[1] for r in render_seq]
                        cat_span = {}
                        _j = 0
                        while _j < len(seq_cats):
                            _k = _j
                            while _k + 1 < len(seq_cats) and seq_cats[_k + 1] == seq_cats[_j]:
                                _k += 1
                            cat_span[_j] = _k - _j + 1
                            _j = _k + 1

                        # 메일 클라이언트는 <style> CSS를 무시하므로 각 셀에 inline style로 직접 지정
                        # (padding/font-size/nowrap도 <style> 값과 동일하게 inline — 메일·포워딩 표시 통일)
                        _SB_BD = 'border:1px solid #2c2c2c;'      # 셀 구분선(inline)
                        _SB_PAD = 'padding:4px 6px; white-space:nowrap;'
                        _sb_waf_w = 40      # wafer 셀 폭(숫자 잘림 방지) inline min-width
                        _SB_WAF = (f'{_SB_BD} text-align:center; width:{_sb_waf_w}px; min-width:{_sb_waf_w}px; '
                                   f'max-width:{_sb_waf_w}px; padding:2px 1px; font-size:10px; white-space:nowrap;')
                        _SB_CAT = f'{_SB_BD} {_SB_PAD} text-align:center; min-width:77px;'      # category 고정열
                        _SB_ITEM = f'{_SB_BD} {_SB_PAD} text-align:center; min-width:240px;'    # Item 고정열
                        sb_html = ''
                        # lot 그룹(헤더 colspan용): _wcols 순서대로 같은 lot을 묶음
                        _lot_groups = []   # [(lot, [col, ...]), ...]
                        for _c in _wcols:
                            if _lot_groups and _lot_groups[-1][0] == _c[0]:
                                _lot_groups[-1][1].append(_c)
                            else:
                                _lot_groups.append((_c[0], [_c]))

                        sb_html += '<table class="score-board" style="border-collapse:collapse; font-size:11px;">\n  <thead>\n'
                        sb_html += '    <tr>\n'
                        sb_html += f'      <th colspan="2" class="sb-frozen-lot" style="{_SB_BD} {_SB_PAD} text-align:center; background-color:#d9e1f2;">LOT_ID</th>\n'
                        # root_lot_id가 같은 형제 lot을 각각 헤더로 분리 (target lot은 강조)
                        for _lot, _cols in _lot_groups:
                            _is_tgt = (str(_lot) == str(target_lot_id))
                            _bg = '#dbe7c8' if _is_tgt else '#f0f0f0'
                            _fw = 'bold' if _is_tgt else 'normal'
                            sb_html += (f'      <th colspan="{len(_cols)}" style="{_SB_BD} {_SB_PAD} text-align:center; '
                                        f'background-color:{_bg}; font-weight:{_fw};">{_lot}</th>\n')
                        sb_html += '    </tr>\n'
                        sb_html += '    <tr>\n'
                        sb_html += f'      <th class="sb-cat" style="{_SB_CAT} background-color:#d9e1f2;">category</th>\n'
                        sb_html += f'      <th class="sb-item" style="{_SB_ITEM} background-color:#d9e1f2;">Item</th>\n'
                        for col in _wcols:
                            sb_html += f'      <th class="sb-waf" style="{_SB_WAF} background-color:#f0f0f0;">#{col[1]}</th>\n'
                        sb_html += '    </tr>\n  </thead>\n  <tbody>\n'

                        for _i, (kind, cat, item, payload) in enumerate(render_seq):
                            sb_html += '    <tr>\n'
                            if _i in cat_span:
                                sb_html += f'      <td class="sb-cat row_heading" rowspan="{cat_span[_i]}" style="{_SB_CAT} font-weight:bold; background-color:#ebf4ff; vertical-align:middle;">{cat}</td>\n'
                            row = payload
                            sb_html += (f'      <td class="sb-item row_heading" style="{_SB_ITEM} font-weight:bold; '
                                        f'background-color:#ebf4ff;">{display_name(item)}</td>\n')
                            for col in _wcols:
                                val = row[col]
                                if pd.isna(val) or val == "":
                                    sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:{GLOBAL_CONFIG.score_color_na};"></td>\n'
                                else:
                                    # 연속 색상(PPT와 동일), ITEM별 스케일 override 지원
                                    bg_color, color = GLOBAL_CONFIG.score_color(val, item)
                                    sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:{bg_color}; color:{color}; font-weight:bold;">{val:.1f}</td>\n'
                            sb_html += '    </tr>\n'
                        sb_html += '  </tbody>\n</table>\n'

                        # Score Board WF MAP은 용량 문제로 제거됨 — PPT에서만 확인.
                        score_board_html = sb_html

                        # ==================== Inline Table HTML 렌더링 (Manual) ====================
                        inlinedata_filtered_pivot = inlinedata_filtered_pivot.reset_index()

                        # 열 순서: Module, Step desc, ITEMNAME, Item (그 뒤 UCL/CL/LCL/wafer)
                        _head_cols = ['Module', 'Step desc', 'ITEMNAME', 'Item']
                        cols = _head_cols + [c for c in inlinedata_filtered_pivot.columns if c not in _head_cols + ['STEP_DESC_ITEM_ID']]
                        inlinedata_filtered_pivot = inlinedata_filtered_pivot[cols]

                        # Module 열 연속 동일값 rowspan 병합 (위아래 병합) — 그룹 첫 행에서만 셀 출력
                        _mods = [str(r['Module']) for _, r in inlinedata_filtered_pivot.iterrows()]
                        _mod_span = {}
                        _mj = 0
                        while _mj < len(_mods):
                            _mk = _mj
                            while _mk + 1 < len(_mods) and _mods[_mk + 1] == _mods[_mj]:
                                _mk += 1
                            _mod_span[_mj] = _mk - _mj + 1
                            _mj = _mk + 1

                        # 메일 클라이언트용 inline style (셀 구분선 + 가운데 정렬 + 줄바꿈 방지)
                        _IT_BD = 'border:1px solid #2c2c2c;'
                        _IT_CTR = 'text-align:center !important; white-space:nowrap;'   # 헤더 CSS(left) override + nowrap
                        # wafer 열: 고정 56px min-width(빈 여백 큼) 제거 → 셀이 내용(#번호/측정값)에 딱 맞게 줄어들고
                        # 좌우 padding(=여백)만 숫자 ~1.5자(≈8px)씩 남겨 컴팩트하게. (auto-layout이라 긴 값은 알아서 확장)
                        _IT_WAF = 'min-width:22px; padding:4px 8px;'
                        _IT_PAD = 'padding:4px 10px;'   # Module~LCL 열 좌우 여백(약 1.5자)

                        it_html = '<table class="inline-table" style="border-collapse:collapse; font-size:11px;">\n'
                        it_html += '  <thead>\n'
                        it_html += '    <tr>\n'
                        for col in inlinedata_filtered_pivot.columns:
                            if col in _head_cols:
                                it_html += f'      <th class="row_heading" style="{_IT_BD} {_IT_CTR} {_IT_PAD} background-color:#e2efda !important;">{col}</th>\n'
                            elif col in ['UCL', 'CL', 'LCL']:
                                it_html += f'      <th style="{_IT_BD} {_IT_CTR} {_IT_PAD} background-color:#f0f0f0 !important;">{col}</th>\n'
                            else:
                                col_str = str(col) if str(col).startswith('#') else '#' + str(col)
                                it_html += f'      <th style="{_IT_BD} {_IT_CTR} {_IT_WAF} background-color:#f0f0f0 !important;">{col_str}</th>\n'
                        it_html += '    </tr>\n'
                        it_html += '  </thead>\n'
                        it_html += '  <tbody>\n'
                        for _ri, (_, row) in enumerate(inlinedata_filtered_pivot.iterrows()):
                            # 짝수행 zebra 배경을 inline으로(브라우저 nth-child(even) CSS와 동일값 — 메일 표시 통일)
                            _zebra = ' style="background-color:#fafbfc;"' if _ri % 2 == 1 else ''
                            it_html += f'    <tr{_zebra}>\n'
                            for col in inlinedata_filtered_pivot.columns:
                                # Module 열은 연속 동일값 rowspan 병합 → 그룹 첫 행에서만 출력
                                if col == 'Module':
                                    if _ri not in _mod_span:
                                        continue
                                    _span_attr = f' rowspan="{_mod_span[_ri]}"' if _mod_span[_ri] > 1 else ''
                                else:
                                    _span_attr = ''

                                val = row[col]
                                if pd.isna(val):
                                    formatted_val = ""
                                elif isinstance(val, (int, float)) and abs(val) >= 1e6:
                                    formatted_val = f"{val:.2e}"
                                elif isinstance(val, (int, float)):
                                    if abs(val) < 0.01 and val != 0:
                                        formatted_val = f"{val:.5g}"
                                    else:
                                        formatted_val = f"{val:.2f}"
                                else:
                                    formatted_val = str(val)

                                # Item명은 표시용 후처리(접두/접미 제거·치환) 적용
                                if col == 'Item' and formatted_val:
                                    formatted_val = display_name(formatted_val)

                                if col in ['UCL', 'CL', 'LCL']:
                                    style = f'{_IT_BD} {_IT_CTR} {_IT_PAD} background-color:#e0f7fa;'
                                elif col in _head_cols:
                                    style = f'{_IT_BD} {_IT_CTR} {_IT_PAD} vertical-align:middle; background-color:#f0fff4;'
                                else:
                                    # wafer 값 셀: LCL/UCL 벗어나면 셀 배경 빨강 강조
                                    _cellbg = ''
                                    if not pd.isna(val):
                                        try:
                                            _v = float(val)
                                            _ucl = row.get('UCL'); _lcl = row.get('LCL')
                                            if (pd.notna(_ucl) and _v > float(_ucl)) or (pd.notna(_lcl) and _v < float(_lcl)):
                                                _cellbg = 'background-color:#ff4d4d; color:#ffffff; font-weight:bold;'
                                        except (ValueError, TypeError):
                                            pass
                                    style = f'{_IT_BD} {_IT_CTR} {_IT_WAF} {_cellbg}'

                                it_html += f'      <td{_span_attr} style="{style}">{formatted_val}</td>\n'
                            it_html += '    </tr>\n'
                        it_html += '  </tbody>\n'
                        it_html += '</table>\n'
                        inline_table_html = it_html

                        # ==================== Lot Detail Table HTML 렌더링 (Manual) ====================
                        # pandas Styler.to_html()은 class/<style> 기반이라 메일에서 깨짐 → inline style로 직접 생성
                        _LD_BD = 'border:1px solid #2c2c2c;'
                        _LD_CELL = f'{_LD_BD} text-align:center; padding:3px 10px; white-space:nowrap;'   # 열 좌우 여백 10px
                        def _ld_esc(_x):
                            return str(_x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        lot_detail_html = '<table class="lot-detail-table" style="border-collapse:collapse; font-size:11px;">\n  <thead>\n    <tr>\n'
                        for _c in et_log.columns:
                            lot_detail_html += f'      <th style="{_LD_CELL} background-color:#e8edf3; font-weight:bold;">{_ld_esc(_c)}</th>\n'
                        lot_detail_html += '    </tr>\n  </thead>\n  <tbody>\n'
                        for _ldi, (_, _r) in enumerate(et_log.iterrows()):
                            _zebra = ' style="background-color:#fafbfc;"' if _ldi % 2 == 1 else ''
                            lot_detail_html += f'    <tr{_zebra}>\n'
                            for _c in et_log.columns:
                                _v = _r[_c]
                                _vs = '' if pd.isna(_v) else _ld_esc(_v)
                                lot_detail_html += f'      <td style="{_LD_CELL}">{_vs}</td>\n'
                            lot_detail_html += '    </tr>\n'
                        lot_detail_html += '  </tbody>\n</table>\n'

                        # ==================== [0] Anomaly: 코드 분석 + (선택)AI 다단계 해석 + Trend chart ====================
                        # 코드(analyze_commonality)는 AI 유무와 무관하게 항상 동작하여 통계 Finding을 산출.
                        # use_gpt_summary가 켜져 있고 LLM이 가능하면, 그 Finding을 입력으로 AI 다단계 해석을 곁들임.
                        _top_n = getattr(GLOBAL_CONFIG, 'anomaly_trend_chart_top_n', 3)

                        # 1) 코드 통계 분석 결과(위 1-3b에서 계산) → HTML 요약
                        #    AI on: 지식판정(RULE) 내용은 상단 AI 해석 블록(_ai_block)에만 표시하고,
                        #           그 아래에는 '이상 N건 · 주의 N건' 간단 요약만(지식판정 상세 목록/‘몇 pt’ 제거).
                        #    AI off: 기존과 동일(전체 이상/주의 목록 요약).
                        code_summary_html = ""
                        try:
                            if _ai_on:
                                code_summary_html = render_findings_count_html(code_findings)
                            else:
                                code_summary_html = render_findings_html(code_findings)
                        except Exception as ce:
                            print(f"[WARN] findings 렌더 스킵 (오류): {ce}")

                        # 2) Anomaly Trend chart 항목 선정 — '통계 기반 자동 분석'(code_findings) 상위와 동일.
                        #    findings(severity 정렬)에서 항목을 순서대로 추출(콤마 분해·중복 제거),
                        #    차트 가능한(merged_df 컬럼 + Trend PNG 존재) 항목만 최대 _top_n개.
                        #    findings로 부족하면 metrics 우선순위(spec_out→deviation)로 보충.
                        def _has_png(_it):
                            _safe = re.sub(r'[\\/:*?"<>|]', '_', str(_it))
                            return os.path.exists(f"RUN/TEMP/{_safe}.png")
                        # anomaly_exclude_items(+WF MAP 제외 키워드) → 이상/주의 차트에서 완전히 제외
                        _excl_items = list(getattr(GLOBAL_CONFIG, 'anomaly_exclude_items', []) or [])
                        _excl_items += [f"*{str(_k).strip()}*"
                                        for _k in (getattr(GLOBAL_CONFIG, 'wfmap_exclude_keywords', []) or [])
                                        if str(_k).strip()]
                        top_item_names = []
                        _seen = set()

                        # CAT2 중복 제거: 같은 CAT2(예: VTH 계열 VTH_N/VTH_P/VTH_DIFF…)는 '대표 1개'만
                        # Trend chart에 노출(사용자 요구). 이상 4건이라도 CAT2가 2종이면 2개만 나온다.
                        # ALIAS→CAT2 매핑(reformatter). CAT2 없는(빈값) 항목은 dedup 대상 아님(각각 허용).
                        _alias_cat2 = {}
                        try:
                            if 'ALIAS' in reformatter.columns and 'CAT2' in reformatter.columns:
                                for _al, _c2 in zip(reformatter['ALIAS'].astype(str), reformatter['CAT2']):
                                    _c2s = str(_c2).strip()
                                    if _al and _c2s and _c2s.lower() != 'nan':
                                        _alias_cat2[_al] = _c2s
                        except Exception:
                            _alias_cat2 = {}
                        _seen_cat2 = set()

                        def _cat2_of(_it):
                            return _alias_cat2.get(str(_it).strip())

                        def _try_add(_it):
                            _it = str(_it).strip()
                            if (not _it) or (_it in _seen) or (_it not in merged_df.columns) or (not _has_png(_it)) \
                                    or item_excluded(_it, _excl_items):
                                return
                            _c2 = _cat2_of(_it)
                            if _c2 is not None and _c2 in _seen_cat2:   # 같은 CAT2 이미 채택 → 스킵(대표 1개만)
                                return
                            top_item_names.append(_it); _seen.add(_it)
                            if _c2 is not None:
                                _seen_cat2.add(_c2)

                        # (AI on) 지식판정(RULE) 매칭 항목을 MD 규칙 순서대로 '먼저' 배치 →
                        #         위에 적힌(강한) 규칙 항목이 앞, 일반 이상 항목보다 우선.
                        if _ai_on:
                            for _f in (code_findings or []):
                                if _f.get('type') != 'DEFECT_MODE':
                                    continue
                                for _k in (_f.get('rule_matched_keys') or [_f.get('item', '')]):
                                    _try_add(_k)
                                    if len(top_item_names) >= _top_n: break
                                if len(top_item_names) >= _top_n: break

                        # 그다음 일반 이상 항목(SPEC_OUT 등)으로 채움(이미 담긴 매칭 항목은 _seen으로 스킵)
                        for _f in (code_findings or []):
                            for _it in str(_f.get('item', '')).split(','):
                                _try_add(_it)
                                if len(top_item_names) >= _top_n: break
                            if len(top_item_names) >= _top_n: break
                        if len(top_item_names) < _top_n and metrics_dict:
                            _sigma = getattr(GLOBAL_CONFIG, 'anomaly_deviation_sigma', 1.5)
                            _anom = [m for m in metrics_dict.values()
                                     if m.get('spec_out_count', 0) > 0 or m.get('deviation', 0.0) > _sigma]
                            _anom.sort(key=lambda m: (m.get('spec_out_count', 0), m.get('deviation', 0.0)), reverse=True)
                            for m in _anom:
                                _it = m['item']
                                if (_it in _seen) or (_it not in merged_df.columns) or (not _has_png(_it)) \
                                        or item_excluded(_it, _excl_items):
                                    continue
                                _c2 = _cat2_of(_it)
                                if _c2 is not None and _c2 in _seen_cat2:   # 같은 CAT2 이미 채택 → 스킵
                                    continue
                                top_item_names.append(_it); _seen.add(_it)
                                if _c2 is not None:
                                    _seen_cat2.add(_c2)
                                if len(top_item_names) >= _top_n: break
                        print(f"[INFO] Anomaly Trend chart 항목 {len(top_item_names)}개 선정(통계 자동분석 상위): {top_item_names}")

                        # 3) Anomaly Trend chart 렌더 — 이상(SPEC OUT)/주의(WARNING) 2그룹.
                        #    - 이상: spec-out 항목을 1행씩, 좌=Trend / 우=spec-out WF MAP(최대한 많이, target lot 전량 우선)
                        #    - 주의: 나머지 항목을 한 행에 가로로 채워 wrap
                        anomaly_html = ""
                        if GLOBAL_CONFIG.show_anomaly_trend_chart:
                            try:
                                import base64
                                if top_item_names:
                                    _wf_on = getattr(GLOBAL_CONFIG, 'anomaly_wfmap_specout', True)
                                    _wf_max = getattr(GLOBAL_CONFIG, 'anomaly_wfmap_max_count', 25)
                                    # supersample 배율 (Score Board WF MAP 합성 등에서 사용)
                                    _hs = max(1, int(getattr(GLOBAL_CONFIG, 'html_img_scale', 2)))
                                    # spec-out WF MAP 데이터 스코프 = '현재 리포트 제품(main vehicle) + 현재 step'만.
                                    #   merged_df에는 with_vehicle(비교용 다른 제품/vehicle)이 concat되어 있고
                                    #   여러 step이 섞여 있어, 그대로 넘기면 render_specout_wfmaps_b64의 '남는 칸
                                    #   채우기'가 다른 제품/vehicle/타 step의 wafer까지 끌어온다. anomaly 분석
                                    #   모집단(anomaly_engine: MASK==main_vehicle)과 동일 스코프로 한정한다.
                                    _wfmap_src = merged_df
                                    _wf_mask_col = next((c for c in ('MASK', 'mask')
                                                         if c in _wfmap_src.columns), None)
                                    if _wf_mask_col is not None:
                                        _wf_mv = _wfmap_src[_wfmap_src[_wf_mask_col] == vehicle]
                                        if not _wf_mv.empty:
                                            _wfmap_src = _wf_mv
                                    _wf_step_col = next((c for c in ('STEP_ID', 'step_id')
                                                         if c in _wfmap_src.columns), None)
                                    if _wf_step_col is not None:
                                        _wf_ss = _wfmap_src[_wfmap_src[_wf_step_col].astype(str)
                                                            == str(target_DC_step_id)]
                                        if not _wf_ss.empty:
                                            _wfmap_src = _wf_ss

                                    def _html_table(cells, ncol, cellpad=2, cellstyle='vertical-align:top;',
                                                    colwidth=None):
                                        """cell HTML 리스트를 ncol개씩 table 행으로 배치(포워딩에서 flex/grid 대체).
                                        템플릿 전역 CSS(table/td 테두리)를 inline border:none로 덮어 격자/구분선 제거.
                                        colwidth(px)를 주면 td/table에 고정 폭 + table-layout:fixed를 걸어
                                        포워딩(메일 클라이언트 재해석) 시 셀이 줄바꿈/축소되지 않게 한다."""
                                        _wstyle = f'width:{colwidth}px; ' if colwidth else ''
                                        _rows = ''
                                        for _i in range(0, len(cells), ncol):
                                            _tds = ''.join(f'<td style="border:none; {_wstyle}{cellstyle}">{_c}</td>'
                                                           for _c in cells[_i:_i + ncol])
                                            _rows += f'<tr>{_tds}</tr>'
                                        # 고정 폭: 열 폭 + 좌우 cellpadding(각 셀 2*cellpad) 합 → 표 전체 px 명시
                                        _tstyle = (f'width:{(colwidth + 2 * cellpad) * ncol}px; table-layout:fixed; '
                                                   if colwidth else '')
                                        return (f'<table role="presentation" cellpadding="{cellpad}" cellspacing="0" '
                                                f'style="border-collapse:collapse; border:none; {_tstyle}">{_rows}</table>')

                                    def _status_badge(is_spec):
                                        """상태 스티커(SPEC OUT/WARNING) span.
                                        포워딩 강건성: 예전엔 차트 위 position:absolute 오버레이였으나,
                                        Outlook 등 mail 클라이언트가 position을 무시해 배지가 정상 흐름의
                                        블록이 되어 차트를 아래로 밀어냈다. → 항목명 헤더 줄에 인라인 배치.
                                        (box-shadow/position 미사용 — 메일·포워딩에서 안정적으로 렌더)."""
                                        if is_spec:
                                            _stat, _bg, _fg = 'SPEC OUT', '#d32f2f', '#ffffff'
                                        else:
                                            _stat, _bg, _fg = 'WARNING', '#f9a825', '#1a1a1a'
                                        return (
                                            f'<span style="display:inline-block; background:{_bg}; color:{_fg}; '
                                            f'font-size:10px; font-weight:bold; padding:2px 7px; border-radius:3px; '
                                            f'margin-right:7px; vertical-align:middle;">{_stat}</span>')

                                    def _trend_block(item, is_spec, img_src, w, h):
                                        # img_src = 완성된 data URI(_img_datauri 결과, PNG 또는 용량 초과 시 JPEG)
                                        # 포워딩 호환: img width/height를 attribute + inline px 둘 다 명시(%/max-width/
                                        # position 미사용). 상태 스티커는 _status_badge로 항목명 헤더에 인라인 배치.
                                        return (
                                            f'<img src="{img_src}" width="{w}" height="{h}" border="0" '
                                            f'style="display:block; width:{w}px; height:{h}px; border:1px solid #ddd;"/>')

                                    def _spec_bounds(item):
                                        _slow = _shigh = None
                                        if item in spec_data.index:
                                            if 'SPECLOW' in spec_data.columns:
                                                _v = spec_data.loc[item, 'SPECLOW']; _slow = None if pd.isna(_v) else _v
                                            if 'SPECHIGH' in spec_data.columns:
                                                _v = spec_data.loc[item, 'SPECHIGH']; _shigh = None if pd.isna(_v) else _v
                                            if 'REPORT DIRECTION' in spec_data.columns:
                                                _dv = str(spec_data.loc[item, 'REPORT DIRECTION']).strip().upper()
                                                if _dv == 'UPPER': _slow = None
                                                elif _dv == 'LOWER': _shigh = None
                                        return _slow, _shigh

                                    def _img_px(img_path, target_w):
                                        """원본 비율 유지하며 target_w(px)에 맞는 (w,h) 반환(포워딩용 고정 px)."""
                                        try:
                                            from PIL import Image as _PILImg
                                            with _PILImg.open(img_path) as _im:
                                                _iw, _ih = _im.size
                                            return target_w, max(1, round(target_w * _ih / _iw))
                                        except Exception:
                                            return target_w, round(target_w * 0.44)

                                    def _img_datauri(raw, max_kb=None):
                                        """인라인 <img>용 data URI 생성 — 이미지 1개 바이트를 상한 이하로 보장.
                                        사내 메일 서버는 큰 인라인 data:image를 '첨부'로 분리해, 제품(=이미지
                                        크기)마다 인라인/첨부가 들쭉날쭉해진다. 모든 이미지를 동일 상한 이하로
                                        맞춰 제품과 무관하게 항상 인라인으로 통일한다.
                                          - 상한 이하: 원본 PNG 유지(무손실).
                                          - 초과: 단계적 다운스케일(최적화 PNG) → 그래도 크면 JPEG(품질↓) 재인코딩.
                                        반환: 완성된 'data:image/...;base64,...' 문자열.
                                        """
                                        if max_kb is None:
                                            max_kb = int(getattr(GLOBAL_CONFIG, 'html_inline_img_max_kb', 100) or 100)
                                        _budget = max_kb * 1024
                                        if len(raw) <= _budget:
                                            return 'data:image/png;base64,' + base64.b64encode(raw).decode('utf-8')
                                        try:
                                            from PIL import Image as _PILc
                                            import io as _ioc
                                            _im = _PILc.open(_ioc.BytesIO(raw)).convert('RGB')
                                            _w0, _h0 = _im.size
                                            for _sc in (0.8, 0.65, 0.5):
                                                _b = _ioc.BytesIO()
                                                (_im.resize((max(1, int(_w0 * _sc)), max(1, int(_h0 * _sc))), _PILc.LANCZOS)
                                                 .save(_b, format='PNG', optimize=True))
                                                if _b.tell() <= _budget:
                                                    return 'data:image/png;base64,' + base64.b64encode(_b.getvalue()).decode('utf-8')
                                            _last = None
                                            for _q in (72, 58, 45, 35):
                                                _b = _ioc.BytesIO()
                                                _im.save(_b, format='JPEG', quality=_q, optimize=True)
                                                _last = _b.getvalue()
                                                if _b.tell() <= _budget:
                                                    return 'data:image/jpeg;base64,' + base64.b64encode(_last).decode('utf-8')
                                            return 'data:image/jpeg;base64,' + base64.b64encode(_last).decode('utf-8')
                                        except Exception:
                                            return 'data:image/png;base64,' + base64.b64encode(raw).decode('utf-8')

                                    _spec_rows, _warn_items = [], []
                                    for item in top_item_names:
                                        safe_item = re.sub(r'[\\/:*?"<>|]', '_', str(item))
                                        img_path = f"RUN/TEMP/{safe_item}.png"
                                        if not os.path.exists(img_path):
                                            continue
                                        with open(img_path, "rb") as f:
                                            img_b64 = _img_datauri(f.read())   # 상한 이하 인라인 data URI(첨부 분리 방지)
                                        _tw, _th = _img_px(img_path, 380)   # 포워딩용 고정 px(가로 380)
                                        _is_spec = metrics_dict.get(item, {}).get('spec_out_count', 0) > 0
                                        if not _is_spec:
                                            # 주의 항목은 블록 생성을 미룬다 — 이상 유무(=_spec_rows)에 따라
                                            # 항목명 헤더를 붙일지 결정(요청: 이상 없이 주의만일 때 항목명 표기).
                                            _warn_items.append((item, img_b64, _tw, _th))
                                            continue
                                        # 이상(SPEC OUT) — 우측에 spec-out WF MAP을 PIL로 1장 합성
                                        # Trend(좌) 1장 + WF MAP 합성(우) 1장 = 아이템당 img 태그 2개
                                        _wf_block = ''
                                        _wf_w = 0   # WF MAP 합성 이미지 표시 폭(px) — 표 고정폭 계산용(포워딩 강건)
                                        if _wf_on:
                                            try:
                                                _slow, _shigh = _spec_bounds(item)
                                                _wfmaps = render_specout_wfmaps_b64(
                                                    _wfmap_src, item, spec_low=_slow, spec_high=_shigh,
                                                    target_lot=target_lot_id, max_maps=_wf_max,
                                                    main_vehicle=vehicle)
                                                if _wfmaps:
                                                    # PIL 합성: '해당 lot(target)' WF MAP은 왼쪽에 파란 테두리 블록으로
                                                    # 묶어 표시하고, 오른쪽에 나머지 lot(tkout_time 최신순) 그리드를
                                                    # 이어붙여 1장으로 만든다. (각 블록 2행 기준 그리드)
                                                    from PIL import Image as _PILImg2, ImageDraw as _PILDraw2, ImageFont as _PILFont2
                                                    import io as _io2
                                                    _map_sz = 58 * _hs   # 맵 1개 px (supersample 적용)
                                                    _lab_h = 14 * _hs    # 라벨 높이
                                                    _pad = 3 * _hs       # 셀 간격
                                                    _cell_w = _map_sz + _pad
                                                    _cell_h = _map_sz + _lab_h + _pad
                                                    _wf_tgt = [w for w in _wfmaps if (len(w) > 2 and w[2])]
                                                    _wf_rest = [w for w in _wfmaps if not (len(w) > 2 and w[2])]
                                                    _bpad = 4 * _hs      # 파란 테두리와 맵 사이 여백
                                                    _bw2 = max(2, _hs)   # 파란 테두리 두께(px)
                                                    _gap = 7 * _hs if (_wf_tgt and _wf_rest) else 0   # 블록 간 간격

                                                    def _grid_dims(_n):
                                                        """2행 기준 그리드 (열수, 행수, 콘텐츠 w, 콘텐츠 h)."""
                                                        if _n <= 0:
                                                            return 0, 0, 0, 0
                                                        _nc = max(1, -(-_n // 2))
                                                        _nr = -(-_n // _nc)
                                                        return (_nc, _nr,
                                                                (_nc - 1) * _cell_w + _map_sz,
                                                                (_nr - 1) * _cell_h + _map_sz + _lab_h)

                                                    _nc_t, _nr_t, _w_t, _h_t = _grid_dims(len(_wf_tgt))
                                                    _nc_r, _nr_r, _w_r, _h_r = _grid_dims(len(_wf_rest))
                                                    # 두 블록의 맵 상단은 같은 높이로 정렬(테두리 여백은 target 블록만)
                                                    _y0 = _pad + (_bpad if _wf_tgt else 0)
                                                    _x_t = _pad + (_bpad if _wf_tgt else 0)
                                                    _x_r = (_x_t + _w_t + _bpad + _gap) if _wf_tgt else _pad
                                                    _cw_total = ((_x_r + _w_r + _pad) if _wf_rest
                                                                 else (_x_t + _w_t + _bpad + _pad))
                                                    _ch_total = max(_y0 + _h_t + (_bpad if _wf_tgt else 0),
                                                                    _y0 + _h_r) + _pad
                                                    # 폰트
                                                    try:
                                                        _cf = "NanumGothic.ttf"
                                                        try:
                                                            _PILFont2.truetype(_cf, 10)
                                                        except Exception:
                                                            _cf = "arial.ttf"
                                                        _cfont = _PILFont2.truetype(_cf, max(9, 10 * _hs))
                                                    except Exception:
                                                        _cfont = _PILFont2.load_default()
                                                    # target lot 라벨용 bold 폰트(없으면 stroke로 두껍게 폴백)
                                                    _cfont_b = None
                                                    for _bf in ("NanumGothicBold.ttf", "malgunbd.ttf", "arialbd.ttf"):
                                                        try:
                                                            _cfont_b = _PILFont2.truetype(_bf, max(9, 10 * _hs))
                                                            break
                                                        except Exception:
                                                            continue
                                                    _comp = _PILImg2.new('RGB', (_cw_total, _ch_total), (255, 255, 255))
                                                    _cdraw = _PILDraw2.Draw(_comp)

                                                    def _draw_wf_cell(_wf, _ox, _oy):
                                                        """WF MAP 1셀(맵+라벨) 드로잉 — target lot 라벨은 파란색+bold."""
                                                        _lab, _b = _wf[0], _wf[1]
                                                        _is_tgt = _wf[2] if len(_wf) > 2 else False
                                                        _wf_img = _PILImg2.open(_io2.BytesIO(base64.b64decode(_b)))
                                                        _wf_img = _wf_img.resize((_map_sz, _map_sz), _PILImg2.LANCZOS)
                                                        _comp.paste(_wf_img, (_ox, _oy))
                                                        _lcolor = (0, 51, 204) if _is_tgt else (85, 85, 85)
                                                        _lfont = (_cfont_b or _cfont) if _is_tgt else _cfont
                                                        try:
                                                            _lbox = _cdraw.textbbox((0, 0), _lab, font=_lfont)
                                                            _lw = _lbox[2] - _lbox[0]
                                                        except Exception:
                                                            _lw = len(_lab) * 6 * _hs
                                                        _lx = _ox + (_map_sz - _lw) // 2
                                                        _ly = _oy + _map_sz + 1 * _hs
                                                        if _is_tgt and _cfont_b is None:
                                                            # bold 폰트가 없으면 stroke(외곽선)로 두껍게
                                                            try:
                                                                _cdraw.text((_lx, _ly), _lab, fill=_lcolor, font=_lfont,
                                                                            stroke_width=max(1, _hs // 2), stroke_fill=_lcolor)
                                                            except TypeError:   # 구버전 Pillow → 1px offset 이중 드로잉
                                                                _cdraw.text((_lx, _ly), _lab, fill=_lcolor, font=_lfont)
                                                                _cdraw.text((_lx + 1, _ly), _lab, fill=_lcolor, font=_lfont)
                                                        else:
                                                            _cdraw.text((_lx, _ly), _lab, fill=_lcolor, font=_lfont)

                                                    for _wi, _wf in enumerate(_wf_tgt):
                                                        _draw_wf_cell(_wf, _x_t + (_wi % _nc_t) * _cell_w,
                                                                      _y0 + (_wi // _nc_t) * _cell_h)
                                                    for _wi, _wf in enumerate(_wf_rest):
                                                        _draw_wf_cell(_wf, _x_r + (_wi % _nc_r) * _cell_w,
                                                                      _y0 + (_wi // _nc_r) * _cell_h)
                                                    # 해당 lot(target) WF MAP 묶음 = 파란 테두리 박스
                                                    if _wf_tgt:
                                                        _cdraw.rectangle(
                                                            [_x_t - _bpad, _y0 - _bpad,
                                                             _x_t + _w_t + _bpad - 1, _y0 + _h_t + _bpad - 1],
                                                            outline=(0, 51, 204), width=_bw2)
                                                    # 최종 크기 (supersample → 표시용 축소)
                                                    _disp_w = _cw_total // _hs
                                                    _disp_h = _ch_total // _hs
                                                    _cbuf = _io2.BytesIO()
                                                    _comp.save(_cbuf, format='PNG', optimize=True)
                                                    _wf_src = _img_datauri(_cbuf.getvalue())   # 상한 이하 인라인(첨부 분리 방지)
                                                    _wf_w = _disp_w   # 표 고정폭 계산용(포워딩 강건)
                                                    _wf_block = (
                                                        f'<img src="{_wf_src}" '
                                                        f'width="{_disp_w}" height="{_disp_h}" border="0" '
                                                        f'style="display:block; width:{_disp_w}px; height:{_disp_h}px; border:none;"/>')
                                            except Exception as _we:
                                                print(f"[WARN] spec-out WF MAP 스킵 ({item}): {_we}")
                                        # 이상 항목명(헤더) → SPEC OUT 배지 + 항목명. Trend(좌) 1장 + WF MAP 합성(우) 1장.
                                        # 배지는 오버레이 대신 헤더 인라인(포워딩 강건: _status_badge 참조).
                                        _item_hdr = (
                                            '<div style="font-size:13px; font-weight:bold; color:#1f4e79; '
                                            'text-align:left; margin:2px 0 3px 2px; '
                                            'border-left:4px solid #d32f2f; padding-left:7px;">'
                                            f'{_status_badge(True)}{display_name(item)}</div>')
                                        # 포워딩 강건: table/td 고정 폭 + table-layout:fixed → 셀 줄바꿈/축소 방지.
                                        _trend_cell_w = _tw + 12                # trend img(_tw) + padding-right(12)
                                        _wf_cell_w = _wf_w if _wf_w else _tw    # WF MAP 합성 폭(없으면 폴백)
                                        _spec_rows.append(
                                            '<div style="margin-bottom:14px;">' + _item_hdr +
                                            '<table role="presentation" cellpadding="0" cellspacing="0" '
                                            f'style="border-collapse:collapse; border:none; '
                                            f'width:{_trend_cell_w + _wf_cell_w}px; table-layout:fixed;">'
                                            f'<tr><td style="border:none; vertical-align:top; padding-right:12px; '
                                            f'width:{_trend_cell_w}px;">{_trend_block(item, True, img_b64, _tw, _th)}</td>'
                                            f'<td style="border:none; vertical-align:top; width:{_wf_cell_w}px;">{_wf_block}</td>'
                                            '</tr></table></div>')

                                    # 주의 블록 조립 — 이상 항목처럼 각 주의 항목도 항상 '항목명 헤더'를
                                    # 위에 붙여 무엇이 주의인지 식별 가능하게 한다. (이전엔 이상 항목이 하나도
                                    # 없을 때만 이름을 붙여, 이상+주의가 섞인 제품에선 주의 차트에 항목명이
                                    # 표시되지 않는 문제가 있었다. → 이상 유무와 무관하게 항상 표기.)
                                    _warn_blocks = []
                                    _warn_col_w = 380   # 주의 차트 폭(= _img_px(...,380)) — 그리드 고정폭용
                                    for _wit, _wb64, _ww, _wh in _warn_items:
                                        _blk = _trend_block(_wit, False, _wb64, _ww, _wh)
                                        # 항목명 헤더는 좌측 정렬(전역 td{text-align:center} 상속 차단).
                                        # WARNING 배지는 오버레이 대신 헤더 인라인(포워딩 강건: _status_badge 참조).
                                        _warn_hdr = (
                                            '<div style="font-size:13px; font-weight:bold; color:#1f4e79; '
                                            'text-align:left; margin:2px 0 3px 2px; '
                                            'border-left:4px solid #f9a825; padding-left:7px;">'
                                            f'{_status_badge(False)}{display_name(_wit)}</div>')
                                        _blk = '<div style="text-align:left;">' + _warn_hdr + _blk + '</div>'
                                        _warn_blocks.append(_blk)

                                    # '이상'/'주의' 탭 라벨은 표시하지 않는다. 각 항목명 헤더 줄의
                                    # SPEC OUT / WARNING 스티커가 상태 식별 역할을 대신한다(포워딩 강건성을
                                    # 위해 차트 위 오버레이 대신 헤더 줄에 인라인 배치 — _status_badge 참조).
                                    _parts = []
                                    if _spec_rows:
                                        _parts.extend(_spec_rows)
                                    if _warn_blocks:
                                        # 주의 차트 — 개별 <img> 태그로 표시 (PIL 합성 → 큰 이미지 → 첨부 분리 방지).
                                        # colwidth로 그리드 셀 고정폭 → 포워딩 시 줄바꿈/축소 방지.
                                        _parts.append(_html_table(_warn_blocks, 2, cellpad=4,
                                                                  cellstyle='vertical-align:top; text-align:left;',
                                                                  colwidth=_warn_col_w))
                                    anomaly_html = ''.join(_parts) if _parts else '<p style="margin:4px 0;">이상항목 없음</p>'
                                else:
                                    anomaly_html = '<p style="margin:4px 0;">이상항목 없음</p>'
                            except Exception as ae:
                                print(f"[WARN] 이상 Trend chart 생성 스킵 (오류): {ae}")
                        else:
                            print("[INFO] show_anomaly_trend_chart=False → 이상 Trend chart 스킵")

                        # 4) (선택) AI 다단계 해석 — code_findings를 입력으로. 실패/미사용 시 None → 코드 분석만 표시
                        ai_html = None
                        if (GLOBAL_CONFIG.use_gpt_summary and getattr(GLOBAL_CONFIG, 'use_gpt_multistep', True)
                                and code_findings and _LLM_FN is not None):
                            try:
                                # 검증용 유효 불량모드 = [RULE](NL→JSON)의 comment(불량 모드명) 목록.
                                #   AI Final이 고른 모드를 이 목록과 대조해 표기(할루시네이션 차단).
                                _defect_modes = [
                                    {'num': str(_i + 1), 'mode': (_r.get('comment') or '').strip(),
                                     'when': '', 'comment': '', 'link': ''}
                                    for _i, _r in enumerate(_json_rules or [])
                                    if (_r.get('comment') or '').strip()]
                                ai_html = interpret_with_ai(
                                    code_findings, metrics_dict, _ANOMALY_KNOWLEDGE_TEXT,
                                    _LLM_FN, config=GLOBAL_CONFIG, target_lot_id=target_lot_id,
                                    item_stats=anomaly_item_stats, defect_modes=_defect_modes,
                                    report_key=report_key)
                                print("[INFO] AI 다단계 해석 적용" if ai_html else "[INFO] AI 다단계 해석 결과 없음")
                            except Exception as ae:
                                print(f"[WARN] AI 다단계 해석 스킵 (오류): {ae}")
                        elif not GLOBAL_CONFIG.use_gpt_summary:
                            print("[INFO] use_gpt_summary=False → AI 해석 스킵 (코드 분석만)")

                        # ==================== HTML 조립 ====================
                        sub_title = f'{target_lot_id} / {target_step_merged}'
                        html_content = html_code.replace('sub_title', sub_title)

                        # [0] 섹션 = (AI 다단계 해석 있으면 상단) + 코드 자동 분석(통계 Finding) + Trend chart 그리드
                        # 섹션 제목/컨테이너 여백은 메일 클라이언트(<style> 무시)·포워딩에서도 동일하게
                        # 보이도록 inline style로 지정(class는 브라우저 sticky/스크롤 보조용으로 유지).
                        _SEC_T = ('border-left:4px solid #003366; padding-left:8px; font-size:15px; '
                                  'font-weight:bold; color:#003366; margin-top:20px; margin-bottom:6px;')
                        _TBL_C = 'margin-top:5px; margin-bottom:15px;'
                        _ai_block = (ai_html + '<hr style="border:none;border-top:1px solid #eee;margin:8px 0;">') if ai_html else ''
                        _chart_sub = (f'<div class="section-title" style="{_SEC_T} font-size:13px; margin-top:14px;">'
                                      'Anomaly Trend Chart</div>')
                        # ── 판정 로직 안내 박스(차트 위 고정 표기) — 임계값은 My_config에서 동적 반영 ──
                        _lg_ratio = getattr(GLOBAL_CONFIG, 'anomaly_lot_dispersion_ratio', 2.0)
                        _lg_fls = float(getattr(GLOBAL_CONFIG, 'anomaly_flier_sigma', 3.5) or 0)
                        _lg_flm = int(getattr(GLOBAL_CONFIG, 'anomaly_flier_max_pts', 0) or 0)
                        _lg_dgf = float(getattr(GLOBAL_CONFIG, 'anomaly_disp_min_spec_frac', 0.0) or 0.0)
                        _lg_agg = ', '.join(f'{k}={v}' for k, v in
                                            (getattr(GLOBAL_CONFIG, 'trend_tkout_agg', {}) or {}).items())
                        _lg_agg_txt = (f' (집계 항목 {_lg_agg} 은 site가 아닌 집계값 기준)'
                                       if _lg_agg else '')
                        _lg_fcnt_txt = '1개 이상' if _lg_flm <= 0 else f'1~{_lg_flm}개'
                        _lg_flier_txt = (
                            f'① Flier — wafer median 대비 |값−median|이 보통 wafer 산포의 '
                            f'{_lg_fls:g}σ를 넘는 pt가 {_lg_fcnt_txt} wafer 존재'
                            f' (REPORT DIRECTION 설정 시 해당 방향은 정상 감도, 반대 방향은 1.5배 완화)'
                            if _lg_fls > 0 else '① Flier — OFF')
                        _lg_gate_txt = (f'(절대 산포가 spec 폭의 {_lg_dgf * 100:g}% 이상일 때)'
                                        if _lg_dgf > 0 else '')
                        _chart_logic = (
                            '<div style="font-size:11px; color:#555555; background:#f7f8fa; '
                            'border:1px solid #e3e6ea; border-radius:4px; padding:6px 10px; '
                            'margin:4px 0 8px 0; line-height:1.7; text-align:left;">'
                            '<b style="color:#003366;">판정 로직</b><br>'
                            f'&nbsp;· <span style="background:#d32f2f; color:#ffffff; font-weight:bold; '
                            f'padding:0 5px; border-radius:2px;">SPEC OUT</span> : 해당 lot 측정값 중 '
                            f'spec 이탈 pt가 1개 이상{_lg_agg_txt}<br>'
                            f'&nbsp;· <span style="background:#f9a825; color:#1a1a1a; font-weight:bold; '
                            f'padding:0 5px; border-radius:2px;">WARNING</span> : 설정된 spec 이탈은 없으나 '
                            f'{_lg_flier_txt} ② 산포 확대 — 특정 wafer의 내부 산포가 보통 wafer 산포의 '
                            f'{_lg_ratio:g}배 초과{_lg_gate_txt}<br>'
                            f'&nbsp;· <b>SPEC OUT WF MAP</b> : <span style="color:#0033cc; font-weight:bold;">파란 '
                            f'테두리 박스(파란 bold 라벨) = 해당 측정 lot_id({target_lot_id})내 wafer</span>, '
                            '<span style="color:#555555;">회색 라벨 = 그 외 tkout_time 기준 최근 '
                            'spec-out WF MAP</span></div>')
                        html_content = html_content.replace(
                            '<div id="target0"></div>',
                            f'<div id="target0"><div class="section-title" style="{_SEC_T}">■ [0] Anomaly Summary</div>'
                            f'{_ai_block}{code_summary_html}{_chart_sub}{_chart_logic}{anomaly_html}</div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target1"></div>',
                            f'<div id="target1"><div class="section-title" style="{_SEC_T}">■ [1] Score Board</div>'
                            # Score Board: 컨테이너 스크롤 없이 전체 항목을 한번에 펼침(max-height 없음, overflow visible).
                            # → thead(LOT_ID/wafer 헤더)가 페이지 스크롤 시 상단에 sticky 고정됨(score-board-open 클래스).
                            f'<div class="table-container score-board-open" style="{_TBL_C}">{score_board_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target2"></div>',
                            f'<div id="target2"><div class="section-title" style="{_SEC_T}">■ [2] Inline Table</div>'
                            f'<div class="table-container" style="{_TBL_C}">{inline_table_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target3"></div>',
                            f'<div id="target3"><div class="section-title" style="{_SEC_T}">■ [3] 최근 DC측정자재 상세</div>'
                            f'<div class="table-container" style="{_TBL_C}">{lot_detail_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target4"></div>',
                            ''
                        )

                        # ==================== 인라인 이미지 불변식 검증 (수정 금지) ====================
                        # 불변식: 리포트 HTML의 모든 <img> src는 반드시 data:image(base64) 인라인이어야
                        # 한다(파일 경로/CID 참조 금지 — 메일 본문·포워딩·보관 HTML에서 이미지가 깨짐).
                        # 코드 수정 후 이 검증에서 [ERROR]가 나오면 이미지 삽입부가 잘못 바뀐 것이다.
                        # 새 이미지를 추가할 때는 항상 _img_datauri()를 거쳐 data URI로 넣을 것.
                        _img_srcs = re.findall(r'<img\s[^>]*?src="([^"]*)"', html_content, re.DOTALL)
                        _bad_srcs = [s for s in _img_srcs if not s.startswith('data:image/')]
                        if _bad_srcs:
                            print(f"[ERROR] HTML 인라인 이미지 불변식 위반 — data:image가 아닌 <img> src "
                                  f"{len(_bad_srcs)}개 발견 (이미지가 깨져 보일 수 있음): "
                                  f"{[s[:60] for s in _bad_srcs[:3]]}")
                        else:
                            print(f"[INFO] HTML 인라인 이미지 검증 OK — <img> {len(_img_srcs)}개 모두 data:image 인라인")

                        # ==================== HTML 저장 ====================
                        with open(f'{html_save_path}{fname}', 'w', encoding='utf-8') as hf:
                            hf.write(html_content)
                        print(f'[INFO] HTML 저장 완료: {html_save_path}{fname}')

                        # ==================== 고화질 PPT(EDM) 미사용 ====================

                        # ==================== S3 업로드 (사내 환경 전용) ====================
                        # My_config.use_s3_upload 로 on/off.
                        if not getattr(GLOBAL_CONFIG, 'use_s3_upload', True):
                            print_status("S3 업로드", "off", f"{search_key} → use_s3_upload=False 스킵")
                        elif S3_CONNECT and client:
                            # 개인 이름 경로 없이 bucket_dx 기준 clean key(vehicle/파일명) 사용
                            s3_key = f'{vehicle}/{final_ppt_file_name_DX}'
                            _s3_local = f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}'
                            try:
                                # 동일 key가 이미 있으면 먼저 delete 후 업로드(put)
                                try:
                                    client.delete_object(Bucket=bucket_dx, Key=s3_key)
                                except Exception:
                                    pass
                                client.upload_file(_s3_local, bucket_dx, s3_key)
                                print_status("S3 업로드", "ok", f"{bucket_dx}/{s3_key}")
                            except Exception as s3e:
                                print_status("S3 업로드", "fail", f"{search_key}: {s3e}")
                        else:
                            print_status("S3 업로드", "off", f"{search_key} → S3 미연결 스킵")

                        # ==================== 사내 메일 API 발송 (PPT 첨부 + HTML 본문) ====================
                        # My_config.use_email_send 로 on/off. 생성된 HTML(html_content)은 본문으로,
                        # 첨부파일은 '저화질 PPT 1개만' 전송한다(HTML 파일 첨부 없음).
                        if getattr(GLOBAL_CONFIG, 'use_email_send', False):
                            try:
                                html_code_final = html_content   # 생성된 HTML 코드 문자열

                                # ── 본문 인라인 이미지 정보 ──
                                # 모든 <img>는 data:image(base64) 인라인. 이미지 1개 바이트를
                                # html_inline_img_max_kb 이하로 맞춰(_img_datauri) 메일 서버의 '첨부 분리'를
                                # 막아, 제품과 무관하게 항상 인라인으로 통일한다(초과 시 자동 PNG 축소→JPEG).
                                import re as _re_mail
                                _img_pattern = r'<img\s[^>]*src="data:image/[^"]*"[^>]*/?\s*>'
                                _imgs = _re_mail.findall(_img_pattern, html_code_final, _re_mail.DOTALL)
                                _png_n = html_code_final.count('src="data:image/png')
                                _jpg_n = html_code_final.count('src="data:image/jpeg')
                                print(f"[INFO] 메일 본문 인라인 이미지 {len(_imgs)}개 "
                                      f"(PNG {_png_n} · JPEG {_jpg_n}, 각 ≤{getattr(GLOBAL_CONFIG, 'html_inline_img_max_kb', 100)}KB)")

                                # 수신 그룹(=메일링 xlsx의 시트명). config email_receiver가
                                # 리스트면 리스트의 모든 그룹(시트)에 각각 발송한다.
                                # (기존엔 email_receiver[0] 첫 항목에만 발송해 나머지 시트가 누락됐음)
                                if isinstance(email_receiver, (list, tuple)):
                                    _receiver_groups = [g for g in email_receiver if g]
                                elif email_receiver:
                                    _receiver_groups = [email_receiver]
                                else:
                                    _receiver_groups = []

                                title = f'[HOL] {vehicle} {target_lot_id} {target_step_merged} HOL AUTO REPORT'

                                # 첨부파일은 PPT 1개만 (HTML은 본문 전용 — 파일 첨부하지 않음).
                                # 여러 그룹에 반복 발송하므로 바이트를 한 번만 읽어 재사용한다.
                                _ppt_full = os.path.join(low_qual_ppt_save_path, final_ppt_file_name_DX)
                                with open(_ppt_full, 'rb') as _pf:
                                    _ppt_bytes = _pf.read()
                                headers = {'x-dep-ticket': GLOBAL_CONFIG.get("TICKET")}

                                if not _receiver_groups:
                                    print_status("메일 발송", "off",
                                                 f"{target_lot_id}_{target_DC_step} — email_receiver 비어있음 → 발송 대상 없음")

                                for email_receiver_now in _receiver_groups:
                                    email_list = get_email_list(email_list_path, email_receiver_now)

                                    payload_content = {
                                        "content": f'{html_code_final}',
                                        "receiverList": email_list,
                                        "senderMailAddress": f"{KNOXID}@samsung.com",
                                        "statusCode": "SENT",
                                        "title": f'{title}',
                                    }
                                    payload = {'mailSendString': f'{payload_content}'}
                                    files = [
                                        ('file', (final_ppt_file_name_DX, _ppt_bytes,
                                                  'application/vnd.ms-powerpoint'))
                                    ]

                                    response = requests.request(
                                        "POST", GLOBAL_CONFIG.get("url"),
                                        headers=headers, data=payload, files=files)
                                    _sc = getattr(response, 'status_code', None)
                                    if _sc == 200:
                                        print_status("메일 발송", "ok",
                                                     f"{target_lot_id}_{target_DC_step} [{email_receiver_now}] "
                                                     f"완료 (수신 {len(email_list)}명, HTTP {_sc})")
                                    else:
                                        # 200이 아니면 상세 에러 내용을 터미널에 출력
                                        try:
                                            _body = response.text
                                        except Exception:
                                            _body = '(응답 본문 읽기 실패)'
                                        print_status("메일 발송", "fail",
                                                     f"{target_lot_id}_{target_DC_step} [{email_receiver_now}] — HTTP {_sc}")
                                        print(f"[ERROR] 메일 발송 응답 오류 (HTTP {_sc}) 상세: {_body}")
                            except Exception as _me:
                                print_status("메일 발송", "fail", f"{target_lot_id}_{target_DC_step}: {_me}")
                        else:
                            print_status("메일 발송", "off", "use_email_send=False → 스킵")

                        log_to_file(f"{search_key} Report 발행 완료", query_log)
                        # 소요 시간 + 산출물(HTML/PPT) 용량 출력
                        _elapsed = time.perf_counter() - _t_report_start

                        def _mb(_p):
                            try:
                                return f"{os.path.getsize(_p) / 1024**2:.2f}MB" if os.path.exists(_p) else "N/A"
                            except OSError:
                                return "N/A"
                        _html_mb = _mb(f'{html_save_path}{fname}')
                        _ppt_mb = _mb(f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}')
                        print_status("Report 발행 완료", "ok",
                                     f"{search_key} — 소요 {_elapsed:.1f}s, HTML {_html_mb}, PPT {_ppt_mb}")

                    except Exception as e:
                        print_status("Report 발행 실패", "fail", f"{search_key}: {e}")
                        traceback.print_exc()
                        log_to_file(f"{search_key} Report 발행 실패: {e}", error_log)
                        continue

                    finally:
                        clear_temp_inside_run()
                        clear_anomaly_inside_run()
                        clear_run_temp_files()   # 랏 리포트 완료 후 RUN/TEMP 내부 파일 비우기(폴더 유지)
                        gc.collect()

            else:
                print("[INFO] dc_done_list가 비어있습니다. Report 발행 대상 없음")

        else:
            print(f"[INFO] DB_Setting_mode = {DB_Setting_mode}, report_making = {report_making}")
            print("[INFO] Report 미발행 모드")

        conn.close()

        # ── 규칙 제안 다이제스트(1일 1회) — 규칙 현황·불량모드 통계·미매칭 패턴 제안을
        #    RUN/AI에 저장하고, 메일링 xlsx에 POWER_USER 시트가 있으면 발송(반영 전까지 매일 반복 제안).
        try:
            _maybe_send_rule_digest(_json_rules, _LLM_FN)
        except Exception as _dge:
            print(f"[WARN] 규칙 다이제스트 스킵 (오류): {_dge}")

        shutdown_chart_pool()   # 병렬 렌더링 워커 풀 정리 (atexit에도 등록되어 있으나 명시 종료)
        print(f'[INFO] ============== {vehicle} 전체 프로세스 완료 ==============')

    else:
        print("[ERROR] reformatter 검증 실패. 프로그램 종료.")


if __name__ == "__main__":
    main()
