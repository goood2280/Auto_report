# ----- Python 표준 라이브러리
import gc
import base64
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
from My_Function import _filter_inline_by_vehicle  # import * 는 언더스코어 이름 미포함
from My_config import GLOBAL_CONFIG
from anomaly_engine import analyze_commonality, render_findings_html, render_findings_count_html, item_excluded, compile_nl_to_json

# ==================================================================================================================================
# 외부 LLM 연결 없이 코드 분석만 사용
# ==================================================================================================================================




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


# ----------------------------------------------------------------------------------------------------------------------------------
# Inline Table 데이터 가공
#   Inline은 리포트의 부가 정보([2] Inline Table)이므로, 설정 미기입·쿼리 결과 없음·시트 형식
#   이상 등으로 만들 수 없더라도 리포트 발행과 메일 발송은 그대로 진행되어야 한다.
#   → 실패 시 예외를 올리지 않고 '열만 있는' 빈 표를 돌려준다.
# ----------------------------------------------------------------------------------------------------------------------------------
_INLINE_IDX_NAMES = ['Module', 'Step desc', 'ITEMNAME', 'Item']


def _empty_inline_pivot():
    """Inline Table용 '열(헤더)만 있는' 빈 pivot.

    reset_index() 하면 Module / Step desc / ITEMNAME / Item / UCL / CL / LCL 열만 남고
    행이 0개인 DataFrame이 되어, 렌더링 코드 수정 없이 헤더만 있는 표가 생성된다.
    """
    return pd.DataFrame(
        columns=['UCL', 'CL', 'LCL'],
        index=pd.MultiIndex.from_arrays([[], [], [], []], names=_INLINE_IDX_NAMES))


def _build_inline_pivot(inlinedata, inline_file_path, inline_file_sheet, vehicle):
    """Inline 측정 데이터 + INLINE 설정 시트 → Inline Table용 pivot(멀티인덱스).

    Parameters
    ----------
    inlinedata : pd.DataFrame
        inlinedata_query() 결과. 비어 있으면(설정 미기입/쿼리 실패) 빈 표를 반환.
    inline_file_path, inline_file_sheet : str
        INLINE 설정 엑셀 경로 / 시트명.
    vehicle : str
        현재 리포트 대상 vehicle (설정 시트 VEHICLE 필터용).

    Returns
    -------
    pd.DataFrame
        index=(Module, Step desc, ITEMNAME, Item), columns=[UCL, CL, LCL, wafer...].
        데이터/설정이 없거나 가공 중 오류가 나면 _empty_inline_pivot()(열만 있는 빈 표).
    """
    if inlinedata is None or getattr(inlinedata, 'empty', True):
        print("[WARN] Inline 데이터가 없습니다(설정 미기입 또는 쿼리 결과 없음) — Inline Table은 열만 표시합니다.")
        return _empty_inline_pivot()

    try:
        inlinedata = inlinedata.copy()
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
        # INLINE 설정 시트에 여러 vehicle이 섞여 있어도 현재 리포트 대상(vehicle)
        # 행만 사용 — Inline Table에 다른 vehicle의 step_id/항목이 섞이지 않도록.
        Inline1 = _filter_inline_by_vehicle(Inline1, vehicle)
        inline_filtered = Inline1[Inline1['Key'] == True].copy()
        if inline_filtered.empty:
            # 설정 시트에 Key=True 행이 하나도 없음 → 표시할 Inline 항목 자체가 없다.
            print("[WARN] INLINE 설정 시트에 Key=True 항목이 없습니다 — Inline Table은 열만 표시합니다.")
            return _empty_inline_pivot()
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
        column_map = {old_col: int(old_col) for old_col in inlinedata_filtered_pivot.columns if str(old_col).isdigit()}
        inlinedata_filtered_pivot = inlinedata_filtered_pivot.rename(columns=column_map)

        sorted_columns = sorted([col for col in inlinedata_filtered_pivot.columns if str(col).isdigit()], key=lambda x: int(x))

        inlinedata_filtered_pivot = inlinedata_filtered_pivot[sorted_columns]

        inlinedata_filtered_pivot = pd.merge(inlinedata_spec, inlinedata_filtered_pivot, how='right', on='STEP_DESC_ITEM_ID')

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
        inlinedata_filtered_pivot.index.names = _INLINE_IDX_NAMES
        return inlinedata_filtered_pivot

    except Exception as _ie:
        # Inline Table 하나 때문에 리포트/메일 전체가 중단되지 않도록 빈 표로 대체
        print(f"[WARN] Inline Table 생성 실패 — 열만 표시합니다: {_ie}")
        traceback.print_exc()
        return _empty_inline_pivot()


# ==================================================================================================================================
# 전체 파이프라인 진입점
#   ⚠️ 병렬 차트 렌더링(My_Function의 ProcessPoolExecutor, Windows spawn)이 워커 프로세스에서
#   __main__(이 파일)을 다시 import 하므로, 실행 본문은 반드시 main() + __main__ 가드 안에
#   있어야 한다. (가드가 없으면 워커가 뜰 때마다 쿼리/리포트 발행이 재실행된다.)
# ==================================================================================================================================
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
        return ('data:image/jpeg;base64,' if raw[:2] == b'\xff\xd8' else 'data:image/png;base64,') + base64.b64encode(raw).decode('utf-8')
    from PIL import Image as _PILc
    import io as _ioc
    _im = _PILc.open(_ioc.BytesIO(raw)).convert('RGB')
    for attempt in range(24):
        scale = 0.8 ** attempt
        resized = _im.resize((max(1, int(_im.width * scale)), max(1, int(_im.height * scale))), _PILc.Resampling.LANCZOS)
        _b = _ioc.BytesIO()
        resized.save(_b, format='JPEG', quality=max(20, 80 - attempt * 5), optimize=True)
        if _b.tell() <= _budget:
            return 'data:image/jpeg;base64,' + base64.b64encode(_b.getvalue()).decode('utf-8')
    raise ValueError('인라인 이미지 크기 제한 초과')



def _parse_trigger(argument):
    """TRIGGER[_MODE[_mail]]_<vehicle>_<lot>_<step>; leading underscore optional."""
    value = argument[1:] if argument.startswith('_') else argument
    if not value.startswith('TRIGGER_'):
        return None, argument, None, None, None
    value = value[len('TRIGGER_'):]
    mode = 'TRIGGER'
    for candidate in ('FORCE', 'NORMAL', 'ALL'):
        if value.startswith(candidate + '_'):
            mode, value = candidate, value[len(candidate) + 1:]
            break
    parts = value.rsplit('_', 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError('트리거 형식: _TRIGGER[_MODE[_mail]]_<vehicle>_<lot>_<step>')
    head, lot, step = parts
    mail = None
    if mode in ('FORCE', 'ALL'):
        from pathlib import Path
        vehicles = [p.name[:-len('_reformatter.csv')] for p in Path('reformatter').glob('*_reformatter.csv')]
        vehicle = next((v for v in sorted(vehicles, key=len, reverse=True) if head.endswith('_' + v)), None)
        if vehicle is None:
            raise ValueError('트리거 수신처/vehicle 확인 필요: reformatter 파일과 일치하는 vehicle이 없습니다')
        mail = head[:-(len(vehicle) + 1)].strip()
        if not mail or any(c in mail for c in '\r\n'):
            raise ValueError('트리거 mail 수신처가 비어 있거나 잘못되었습니다')
    else:
        vehicle = head
    return mode, vehicle, lot, step, mail


def _force_viewing_period(log_frame, lot, step, days, now):
    target = log_frame[(log_frame['lot_id'].astype(str) == lot)
                       & (log_frame['dc_step_id'].astype(str) == step)]
    times = pd.to_datetime(target['tkout_time'], errors='coerce').dropna()
    if times.empty:
        raise ValueError(f'FORCE: {lot}_{step} prime key 진행날짜를 log에서 찾지 못했습니다')
    start = pd.Timestamp(now).normalize() - pd.Timedelta(days=days)
    if times.min() < start:
        extended = times.min().normalize() - pd.Timedelta(days=2)
        days = max(days, (pd.Timestamp(now).normalize() - extended).days)
        print(f'[INFO] FORCE {lot}_{step}: trend 시작일 {start.date()} → {extended.date()} '
              f'(prime key 진행날짜 전 2일, {days}일)')
    return days


def _filter_normal_shots(frame, zones):
    flag = next((c for c in zones if str(c).strip().lower() == '13pt'), None)
    if flag is None:
        raise ValueError('NORMAL: Extractor Zone_Define에 13pt 열이 없습니다')
    keys = ['MASK', 'CHIP_X_POS', 'CHIP_Y_POS', 'FLAT_ZONE_POS']
    allowed = zones.loc[zones[flag].astype(str).str.strip().str.lower().isin(['o', 'y', 'true']), keys].drop_duplicates()
    left = ['mask', 'chip_x_pos', 'chip_y_pos', 'flat_zone']
    return frame.merge(allowed.rename(columns=dict(zip(keys, left))), on=left, how='inner', validate='many_to_one')


def _trigger_receivers(path, recipient):
    """Explicit address or exact mailing sheet; never fall back to a different group."""
    if '@' in recipient:
        addresses = [a.strip() for a in recipient.split(',') if a.strip()]
        if not addresses or any(not re.fullmatch(r'[^\s@,]+@[^\s@,]+\.[^\s@,]+', a) for a in addresses):
            raise ValueError('잘못된 트리거 이메일 주소')
        return [dict(email=a, recipientType='TO', seq=i) for i, a in enumerate(addresses, 1)]
    with pd.ExcelFile(path) as book:
        if recipient not in book.sheet_names:
            raise ValueError(f'트리거 수신 그룹 없음: {recipient}')
    return get_email_list(path, recipient)


def _trend_artifacts(charts, title):
    """Re-encode all charts until complete UTF-8 HTML and PPTX meet strict byte caps."""
    import io
    import html
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches, Pt
    ordered = sorted(charts.items(), key=lambda pair: pair[1][0])
    if not ordered:
        raise ValueError('ALL: category에 속하는 측정 항목이 없습니다')
    for attempt in range(18):
        scale = 0.82 ** attempt
        quality = max(20, 85 - attempt * 5)
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        body = ['<!doctype html><html><meta charset="utf-8"><body>', '<h1>' + html.escape(title) + '</h1>']
        category = None
        count = 0
        for name, (cat, raw) in ordered:
            im = Image.open(io.BytesIO(raw)).convert('RGB')
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=quality, optimize=True)
            encoded = buf.getvalue()
            if cat != category:
                category, count = cat, 0
                body.append('<h2>' + html.escape(cat) + '</h2>')
            if count % 6 == 0:
                slide = prs.slides.add_slide(prs.slide_layouts[6])
                tf = slide.shapes.add_textbox(Inches(.3), Inches(.15), Inches(12.7), Inches(.5)).text_frame
                tf.text = cat
                tf.paragraphs[0].font.size = Pt(20)
            slot = count % 6
            x, y = .25 + (slot % 2) * 6.55, .8 + (slot // 2) * 2.2
            label = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(6.3), Inches(.32)).text_frame
            label.text = name
            label.paragraphs[0].font.size = Pt(10)
            slide.shapes.add_picture(io.BytesIO(encoded), Inches(x), Inches(y + .24), width=Inches(5.0))
            uri = _img_datauri(encoded)
            body.append('<div><h3>' + html.escape(name) + '</h3><img width="640" src="' + uri + '"></div>')
            count += 1
        body.append('</body></html>')
        content = ''.join(body)
        ppt = io.BytesIO()
        prs.save(ppt)
        if len(content.encode('utf-8')) < 2_000_000 and ppt.tell() < 10_000_000:
            sources = re.findall(r'<img\s[^>]*?src="([^"]*)"', content, re.DOTALL)
            if len(sources) != len(charts) or any(not src.startswith('data:image/') for src in sources):
                raise ValueError('HTML 인라인 이미지 불변식 위반')
            print('[INFO] HTML 인라인 이미지 검증 OK')
            return content, ppt.getvalue()
    raise ValueError('ALL: 모든 trend를 유지하면서 HTML 2MB/PPTX 10MB 미만으로 축소할 수 없습니다')


def _publish_all_trends(frame, reformatter, vehicle, lot, root, dc, step, recipient, date):
    from pptx import Presentation
    chosen = reformatter.loc[reformatter['CAT2'].notna() & reformatter['CAT2'].astype(str).str.strip().ne('')].copy()
    chosen = chosen.drop_duplicates('ALIAS')
    chosen['REPORT ORDER'] = range(1, len(chosen) + 1)
    spec = chosen.set_index('ALIAS')
    _, _, charts = insert_plots(frame, Presentation(), {}, lot, root, dc, step, spec,
                                reformatter=reformatter, trend_only=True, dpi=150, img_quality=75)
    content, ppt = _trend_artifacts(charts, f'{vehicle} {lot} {step} ALL Trends')
    stem = f'{date}-{vehicle}-{lot}-{step}-ALL-Trends'
    html_path = os.path.join(GLOBAL_CONFIG.get('html_save_path'), stem + '.html')
    ppt_path = os.path.join(GLOBAL_CONFIG.get('low_qual_ppt_save_path'), stem + '.pptx')
    atomic_bytes(html_path,content.encode('utf-8'))
    atomic_bytes(ppt_path,ppt)
    if _RUN and _RUN.current:
        _capture_artifacts(html_path,ppt_path)
        _RUN.stage('email')
    identity=_RUN.current['id'] if _RUN and _RUN.current else stem
    if _RUN and _RUN.current:html_path,ppt_path=_RUN.current['paths']['html'],_RUN.current['paths']['ppt']
    state=_send_report_files(html_path,ppt_path,[recipient],f'[HOL] {vehicle} {lot} {step} ALL Trends',identity)
    if _RUN and _RUN.current:_RUN.current['email']=state
    if state in ('failed','unknown'):raise RuntimeError(f'ALL 메일 발송 {state}')
    if _RUN and _RUN.current:_RUN.finish_report('success')
    print(f'[INFO] ALL {len(charts)} trends: HTML {len(content.encode("utf-8"))} bytes / PPTX {len(ppt)} bytes')



_RUN = None


class OperationRun:
    def __init__(self, argument):
        self.id = os.getenv('AUTO_REPORT_RUN_ID') or uuid.uuid4().hex
        self.started = time.time()
        self.stage_started = time.perf_counter()
        self.data = dict(id=self.id, argument=argument, started=self.started, status='running',
                         vehicle='', stage='startup', timings={}, reports=[], issues=[])
        self.current = None
        self.persist()

    def persist(self):
        self.data['updated'] = time.time()
        ops_put('runs', self.id, self.data)
        atomic_json(os.path.join(operations_root(),'progress',self.id+'.json'), self.data)

    def stage(self, name):
        elapsed=time.perf_counter()-self.stage_started
        previous=self.data['stage']
        self.data['timings'][previous]=self.data['timings'].get(previous,0)+round(elapsed,3)
        if self.current:
            self.current['timings'][previous]=self.current['timings'].get(previous,0)+round(elapsed,3)
            self.save_report()
        self.data['stage']=name;self.stage_started=time.perf_counter();self.persist()

    def save_report(self):
        if self.current:
            self.current['updated']=time.time()
            ops_put('reports',self.current['id'],self.current)

    def begin(self, vehicle, lot, step, mode):
        self.stage('report_prepare')
        pk=f'{vehicle}_{lot}_{step}'
        measurement=ops_get('measurements',pk,{})
        revision=measurement.get('tkout_time','unknown')
        identity=f'{pk}|{mode or "AUTO"}|{revision}'
        # Explicit manual requests are distinct, scheduler retry uses a stable request identity.
        if mode:
            identity+='|'+(os.getenv('AUTO_REPORT_REQUEST_ID') or self.id)
        old=ops_get('reports',identity,{})
        self.current=dict(id=identity,prime_key=pk,vehicle=vehicle,lot=lot,step=step,mode=mode or 'AUTO',
                          tkout_time=revision,started=time.time(),run_id=self.id,attempts=old.get('attempts',0)+1,
                          generated=False,saved=False,email='pending',upload='pending',status='running',
                          reason='',timings={},paths={})
        self.data['reports'].append(identity);self.save_report();self.stage('report_prepare')
        return self.current

    def finish_report(self, status, reason=''):
        if not self.current:return
        self.stage('between_reports')
        if self.current.get('email')=='unknown':status='unknown'
        self.current.update(status=status,reason=reason,elapsed=round(time.time()-self.current['started'],3))
        self.save_report()
        ops_put('report_attempts',self.id+'|'+self.current['id'],self.current)
        self.current=None

    def finish(self, error=None):
        if self.current:self.finish_report('failed',str(error or '발행 결과가 확정되지 않은 채 종료'))
        self.stage('finished')
        reports=[ops_get('reports',key,{}) for key in self.data['reports']]
        failed=bool(error or self.data['issues'] or any(r.get('status') in ('failed','unknown') for r in reports))
        self.data.update(status='failed' if failed else 'success',error=str(error) if error else '',
                         elapsed=round(time.time()-self.started,3))
        self.persist()
        output=os.getenv('AUTO_REPORT_RESULT_PATH')
        if output:atomic_json(output,self.data)
        return 1 if failed else 0



def _retry_candidates(candidates, final_log, vehicle):
    """Called under the product lock: queued/running records belong to interrupted work."""
    revisions={}
    for record in ops_list('reports'):
        if record.get('vehicle')!=vehicle or record.get('mode')!='AUTO':continue
        retry=(record.get('status') in ('queued','running','failed','unknown') or
               (record.get('email')=='disabled' and GLOBAL_CONFIG.get('use_email_send',False)))
        if not retry or record.get('attempts',0)>=int(GLOBAL_CONFIG.get('report_max_attempts',3)):continue
        revisions[(record['prime_key'],record['tkout_time'])]=None
    if not revisions:return candidates
    # Series.astype(str) drops 00:00:00 for all-midnight batches, unlike the ledger.
    keys=zip(final_log['prime_key'].astype(str),final_log['tkout_time'].map(str))
    positions={key:index for index,key in enumerate(keys)}
    matched=final_log.iloc[[positions[key] for key in revisions if key in positions]]
    matched=matched[['lot_id','dc_step_id','dc_done','tkout_time']]
    return pd.concat([candidates,matched],ignore_index=True).drop_duplicates(['lot_id','dc_step_id'])


def _queue_auto_reports(candidates, vehicle):
    """Persist intent before advancing dc_done; historical completed rows are never seeded."""
    records={}
    now=time.time()
    for row in candidates.to_dict('records'):
        pk=f"{vehicle}_{row['lot_id']}_{row['dc_step_id']}"
        revision=str(row['tkout_time'])
        identity=f'{pk}|AUTO|{revision}'
        records[identity]=dict(id=identity,prime_key=pk,vehicle=vehicle,lot=str(row['lot_id']),
                               step=str(row['dc_step_id']),mode='AUTO',tkout_time=revision,
                               status='queued',attempts=0,generated=False,saved=False,email='pending',
                               upload='pending',paths={},timings={},reason='발행 대기',
                               started=now,updated=now,run_id=_RUN.id if _RUN else '')
    existing=ops_get_many('reports',records)
    pending=[(key,value) for key,value in records.items() if key not in existing]
    if pending:ops_many('reports',pending)


def _resume_saved(candidates, vehicle):
    remaining=[]
    for row in candidates.to_dict('records'):
        pk=f"{vehicle}_{row['lot_id']}_{row['dc_step_id']}"
        measure=ops_get('measurements',pk,{})
        identity=f'{pk}|AUTO|{measure.get("tkout_time","unknown")}'
        old=ops_get('reports',identity,{})
        if not old.get('saved') or old.get('email')=='sent':remaining.append(row);continue
        paths=old.get('paths',{})
        if not all(os.path.exists(paths.get(k,'')) for k in ('html','ppt')):
            remaining.append(row);continue
        _RUN.stage('email_retry_saved')
        _RUN.current=dict(old,run_id=_RUN.id,started=time.time(),status='running',timings={},attempts=old.get('attempts',0)+1)
        _RUN.data['reports'].append(identity);_RUN.stage('email_retry_saved')
        try:
            state=_send_report_files(paths['html'],paths['ppt'],GLOBAL_CONFIG.get('email_receiver'),
                                     f"[HOL] {vehicle} {row['lot_id']} {row['dc_step_id']} HOL AUTO REPORT",identity)
            _RUN.current['email']=state
            _RUN.finish_report('success' if state in ('sent','disabled') else state,
                               '기존 저장 파일 재사용' if state=='sent' else '발송 상태 확인/수신처 설정 필요')
        except Exception as exc:
            _RUN.finish_report('failed',str(exc))
            print_status('저장 리포트 재발송','fail',f'{pk}: {exc}')
    return pd.DataFrame(remaining,columns=candidates.columns)


def _capture_artifacts(html_path,ppt_path):
    import zipfile
    if not _RUN or not _RUN.current:return
    with zipfile.ZipFile(ppt_path) as archive:
        if archive.testzip() is not None:raise ValueError('PPTX ZIP 무결성 검증 실패')
    directory=os.path.join(operations_root(),'artifacts',_RUN.id,re.sub(r'[^A-Za-z0-9_.-]','_',_RUN.current['prime_key']))
    paths={}
    for kind,path in [('html',html_path),('ppt',ppt_path)]:
        target=os.path.join(directory,os.path.basename(path))
        with open(path,'rb') as stream:atomic_bytes(target,stream.read())
        paths[kind]=os.path.abspath(target)
    _RUN.current.update(generated=True,saved=True,paths=paths)
    _RUN.save_report()


def _observe_measurements(log_frame, eligible, config):
    selected=set(eligible['lot_id'].astype(str)+'_'+eligible['dc_step_id'].astype(str)) if len(eligible) else set()
    previous=ops_get_many('measurements',log_frame['prime_key'])
    records=[]
    now=time.time()
    for row in log_frame.to_dict('records'):
        pk=str(row['prime_key']); old=previous.get(pk,{})
        tk=str(row.get('tkout_time',''))
        new=(not old or old.get('tkout_time')!=tk)
        key=f"{row['lot_id']}_{row['dc_step_id']}"
        if not config.get('report_making'):reason='report_making=False'
        elif config.get('DB_Setting_mode'):reason='DB_Setting_mode=True'
        elif str(row['lot_id']).startswith('A4') and config.get('ptype_lot_turnoff') in (True,'True'):reason='P-Type 제외 설정'
        elif config.get('specific_dc_layer') is not False and config.get('dc_dict').get(row['dc_step_id'])!='MFDC':reason='specific_dc_layer 제외'
        elif not bool(row.get('dc_done')):reason='측정 완료/대기시간 조건 미충족'
        elif key in selected:reason='발행 대기'
        else:reason='기존 측정 완료 이력; 이번 실행의 신규 발행 대상 아님'
        value=dict(prime_key=pk,vehicle=config.get('vehicle'),lot=str(row['lot_id']),step=str(row['dc_step_id']),
                   tkout_time=tk,first_seen=old.get('first_seen',now),last_seen=now,
                   revision_seen=now if new else old.get('revision_seen',now),reason=reason,
                   wafer_id=str(row.get('wafer_id','')),dc_done=bool(row.get('dc_done')),
                   run_id=_RUN.id if _RUN else '',log_path=config.get('unified_log',''))
        records.append((pk,value))
    if records:ops_many('measurements',records)


def _durable_mail(identity, recipients, title, html_path, ppt_path, config):
    """Persist before sending. Read/connection loss is uncertain and never auto-resubmitted."""
    import hashlib
    if not recipients:raise ValueError('메일 수신처 없음')
    recipient_key=','.join(sorted(r['email'].lower() for r in recipients))
    key=hashlib.sha256((identity+'|'+recipient_key).encode()).hexdigest()
    old=ops_get('mail',key,{})
    if old.get('status')=='sent':return 'sent'
    if old.get('status') in ('sending','unknown'):
        return 'unknown'
    if old.get('attempts',0)>=int(config.get('mail_max_attempts',3)):
        return old.get('status','failed')
    record=dict(id=key,identity=identity,status='sending',attempts=old.get('attempts',0)+1,
                recipients=recipients,title=title,html_path=os.path.abspath(html_path),ppt_path=os.path.abspath(ppt_path) if ppt_path else None,
                vehicle=config.get('vehicle'),started=time.time())
    ops_put('mail',key,record)
    submitted=False
    try:
        with open(html_path,encoding='utf-8') as stream:content=stream.read()
        ppt=None
        if ppt_path:
            with open(ppt_path,'rb') as stream:ppt=stream.read()
        payload=dict(content=content,receiverList=recipients,senderMailAddress=f"{config.get('KNOXID')}@samsung.com",
                     statusCode='SENT',title=title)
        mime='text/csv' if str(ppt_path).lower().endswith('.csv') else 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        submitted=True
        response=requests.request('POST',config.get('url'),headers={'x-dep-ticket':config.get('TICKET')},
                                  data={'mailSendString':str(payload)},
                                  files=[('file',(os.path.basename(ppt_path),ppt,mime))] if ppt_path else [],
                                  timeout=(config.get('mail_connect_timeout_sec',10),config.get('mail_read_timeout_sec',90)))
        code=response.status_code
        record['status']='sent' if code==200 else ('unknown' if code>=500 else 'failed')
        record['reason']=f'HTTP {code}'
    except requests.exceptions.ConnectTimeout:
        record.update(status='retryable',reason='연결 시간 초과 (전송 전)')
    except (requests.exceptions.Timeout,requests.exceptions.ConnectionError) as exc:
        record.update(status='unknown',reason=f'응답 확인 불가: {type(exc).__name__}; 발송 여부 수동 확인 필요')
    except (requests.exceptions.MissingSchema,requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,requests.exceptions.InvalidHeader) as exc:
        record.update(status='failed',reason=str(exc))  # Request validation failed before transport.
    except Exception as exc:
        # Broken chunked responses and other post-submission errors may follow actual delivery.
        record.update(status='unknown' if submitted else 'failed',reason=str(exc))
    record['elapsed']=round(time.time()-record['started'],3);record['updated']=time.time()
    ops_put('mail',key,record)
    return record['status']


def _send_report_files(html_path,ppt_path,receivers,title,identity):
    if not GLOBAL_CONFIG.get('use_email_send',False):return 'disabled'
    groups=receivers if isinstance(receivers,(list,tuple)) else [receivers]
    if not groups or not any(groups):raise ValueError('메일 수신처 없음')
    states=[]
    for group in groups:
        state=_durable_mail(identity,_trigger_receivers(GLOBAL_CONFIG.get('email_list_path'),group),title,html_path,ppt_path,GLOBAL_CONFIG)
        states.append(state)
        print_status('메일 발송','ok' if state=='sent' else 'fail',f'{group}: {state}')
    return 'sent' if all(v=='sent' for v in states) else ('unknown' if 'unknown' in states else 'failed')


def _main_impl():
    global _LOG_PATH

    if len(sys.argv) != 2:
        print("Usage: python main.py <ItemName>")
        sys.exit(1)

    raw_arg = sys.argv[1]
    trigger_flag = False

    # ── CLI: 자연어 규칙 변환 도구 (리포트 생성과 별개) ──
    #   python Main.py --convert-nl-rules      : 변환 결과(자연어→when) 미리보기 + 매핑 캐시 갱신(발행/MD 변경 없음)
    #   python Main.py --convert-nl-rules-md   : 변환해서 '바로' MD의 ANOMALY_RULES에 [RULE]로 적용(확인 없음)
    #   키워드 규칙 변환만 사용. 같은 문구는 캐시로 항상 같은 코드.
    # ── CLI: 규칙 제안 다이제스트 미리보기 (리포트 발행과 별개, 상태 파일/메일 발송 없음) ──
    #   RUN/ARCHIVE 스냅샷을 집계해 터미널에 출력. 실제 저장/발송은 발행 루프 말미에 1일 1회 자동.
    if raw_arg == '--rule-digest':
        from anomaly_engine import build_rule_digest
        _llm = None
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

        _llm = None
        print("[NL] 변환 방식: 키워드 규칙")
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

    trigger_mode, vehicle_name, trigger_lot, trigger_step, trigger_mail = _parse_trigger(raw_arg)
    trigger_flag = trigger_mode is not None
    if trigger_flag:
        raw_arg = f"{vehicle_name}_{trigger_lot}_{trigger_step}"

    # config.yaml에서 설정 로드
    GLOBAL_CONFIG.load_from_yaml(vehicle_name)
    GLOBAL_CONFIG.use_gpt_summary = False
    GLOBAL_CONFIG.use_gpt_multistep = False

    # =============================================== Config get ==================================================================

    vehicle = GLOBAL_CONFIG.get("vehicle")
    _RUN.data["vehicle"]=vehicle
    _RUN.stage("configuration")
    trim_runtime_cache(os.path.join(operations_root(), "cache"))
    inline_file_sheet = GLOBAL_CONFIG.get("inline_file_sheet")

    prod = GLOBAL_CONFIG.get("prod")
    with_vehicle = GLOBAL_CONFIG.get("with_vehicle")
    delay_min = GLOBAL_CONFIG.get("delay_min")
    viewing_period = int(GLOBAL_CONFIG.get("viewing_period"))
    et_log_show = GLOBAL_CONFIG.get("et_log_show")
    test_mode = GLOBAL_CONFIG.get("test_mode")
    DB_Setting_mode = GLOBAL_CONFIG.get("DB_Setting_mode")
    KNOXID = GLOBAL_CONFIG.get("KNOXID")
    email_receiver = [trigger_mail] if trigger_mail else GLOBAL_CONFIG.get("email_receiver")

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
    _use_s3 = not trigger_flag and GLOBAL_CONFIG.get('use_s3_upload', True)
    S3_CONNECT = False
    client = None
    if not _use_s3:
        print_status("S3 드라이브", "off", "기본 AUTO 발행만 업로드 (또는 use_s3_upload=False)")
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

    _LLM_FN = None
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
            _json_rules = compile_nl_to_json(_ANOMALY_KNOWLEDGE_TEXT, _LLM_FN, cache_dir=_ai_dir)
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
            _RUN.stage('et_query')
            etdata_query()
            print('[INFO] ==============et_query 수행완료==============')
            # NOTE: et_LOTWF_generator 삭제됨 — daily DB에서 DuckDB로 직접 조회
            log_to_file("Query Success...", query_log)

            _RUN.stage('wip_query')
            wipdata_query()
            print('[INFO] ==============wip_query 수행완료==============')

        _RUN.stage('measurement_selection')
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

        selected_et_log = final_lot_log[['lot_id', 'dc_step_id', 'dc_done','tkout_time']]
        selected_et_log_before = existing_lot_log[['lot_id', 'dc_step_id', 'dc_done']]
        selected_et_log_before.rename(columns={'dc_done': 'dc_done_before'}, inplace=True)
        selected_et_log = pd.merge(selected_et_log, selected_et_log_before, on=['lot_id','dc_step_id'], how='left')

        # DC 완료여부 판정 Logic
        dc_done_list = selected_et_log[(selected_et_log['dc_done'] != selected_et_log['dc_done_before'])]
        dc_done_list = dc_done_list[dc_done_list['dc_done'] == True]
        if not trigger_flag:dc_done_list=_retry_candidates(dc_done_list,final_lot_log,vehicle)

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
            # 수신처: Scheduler.py가 환경변수 AUTO_REPORT_EMAIL_RECEIVER로 지정하면 그 그룹에만 발송된다
            #        (My_config.load_from_yaml에서 config.yaml의 email_receiver를 덮어씀)
            if os.getenv('AUTO_REPORT_EMAIL_RECEIVER'):
                print(f"[INFO] 트리거 수신 그룹 지정: {email_receiver}")
        print("[INFO] 리포팅 진행할 LOT LIST")
        dc_done_list = pd.DataFrame(dc_done_list)
        _observe_measurements(final_lot_log,dc_done_list,GLOBAL_CONFIG)
        if not trigger_flag and report_making and not DB_Setting_mode:
            _queue_auto_reports(dc_done_list,vehicle)
        # A crash after this checkpoint must leave recoverable publication intent.
        atomic_output(Final_et_log_path, lambda temp: final_lot_log.to_csv(temp, index=False))
        if not trigger_flag and report_making and not DB_Setting_mode:
            dc_done_list=_resume_saved(dc_done_list,vehicle)

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

                if trigger_mode == 'FORCE':
                    viewing_period = _force_viewing_period(
                        final_lot_log, trigger_lot, trigger_step, viewing_period, datetime_now)

                # DuckDB로 viewing_period 범위의 raw 데이터 로드
                _RUN.stage('raw_load')
                raw_df = load_daily_projected(conn, DB_et_daily, viewing_period, reformatter)

                if raw_df.empty:
                    print(f'[WARN] daily DB에 {viewing_period}일 이내 데이터 없음')
                    raise ValueError(f'daily DB에 {viewing_period}일 이내 필요한 측정 데이터 없음')

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

                _RUN.stage('pivot_addp')
                merged_df = raw_df.pivot_table(
                    values='et_value', index=pivot_idx,
                    columns='item_id', aggfunc='last', observed=True
                )

                # ── ADDP (Index) 계산 ──
                _cols_before_addp = set(merged_df.columns)
                merged_df = cached_reformat(merged_df, ALIAS, FORMULA)
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
                            wv_reformatter = pd.read_csv(f'reformatter/{with_vehicle_now}_reformatter.csv')
                            wv_raw_df = load_daily_projected(conn,wv_daily_path,viewing_period,wv_reformatter)
                            if wv_raw_df.empty:
                                print(f'[WARN] {with_vehicle_now} daily DB 데이터 없음, 스킵')
                                continue
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
                            wv_pivot = cached_reformat(wv_pivot, wv_ALIAS, wv_FORMULA)
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
                if trigger_mode == 'ALL':
                    columns_to_include_1 = reformatter['ALIAS'].dropna().tolist()

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
                _watch_columns=reformatter.loc[reformatter['CAT2'].notna(),'ALIAS'].dropna().tolist()
                columns_to_include = columns_to_include_1 + columns_to_include_2 + pchk_keep + derived_addp + _watch_columns
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

                _RUN.stage('coordinate_join')
                # Add coordinate_file
                coordinate_file = pd.read_excel(coordinate_file_path, sheet_name=None, engine='openpyxl')
                zone_define = coordinate_file['Zone_Define']
                zone_define['MASK'] = zone_define['MASK'].replace('RHV_OS','RHV-OS') #RHV OS Vehicle 명 상이함. matching을 위한 변경
                zone_define = zone_define.astype({'CHIP_X_POS': int, 'CHIP_Y_POS': int, 'CHIP_X_ADJ': int, 'CHIP_Y_ADJ': int, 'FLAT_ZONE_POS': int})
                # WF MAP geometry(150mm 원 fit·shot pitch)를 측정 데이터가 아닌 좌표파일의
                # MASK(vehicle)별 전체 chip layout(CHIP_X_ADJ/CHIP_Y_ADJ/Chip_Radius) 기준으로
                # 계산하도록 등록 — 측정 pt가 적은(예 13pt) wafer도 WF MAP이 깨지지 않는다.
                set_chip_layout(zone_define)
                if trigger_mode == 'NORMAL':
                    merged_df = _filter_normal_shots(merged_df, zone_define)
                    print(f"[INFO] NORMAL 13pt 선택 shot: {len(merged_df)}행")


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
                _before_coordinates=len(merged_df)
                merged_df = pd.merge(merged_df,zone_define,on=['MASK','CHIP_X_POS','CHIP_Y_POS','FLAT_ZONE_POS'])
                _RUN.data['coordinate_match']=dict(input_rows=_before_coordinates,matched_rows=len(merged_df))
                # Daily Trend/ML independently read the DB; watchdog only inspects service execution logs.

                html_code = GLOBAL_CONFIG.get("html_code")

                # Description PPT 파싱은 lot과 무관(경로/품질만 의존) → 랏 루프 밖에서 1회만 수행
                description_image_info_dict_low_qual = {} if trigger_mode == 'ALL' else calcaulate_description_image_info_dict(description_ppt_path, img_quality = 20)

                for search_key in search_strings : #search key = match key, fablot_id + dc_step_id
                    try :
                        _t_report_start = time.perf_counter()
                        _lot, _step = search_key.rsplit('_',1)
                        _RUN.begin(vehicle,_lot,_step,trigger_mode)
                        os.environ['AUTO_REPORT_TEMP_DIR']=os.path.join(operations_root(),'temp',_RUN.id,uuid.uuid4().hex)
                        os.makedirs(report_temp_dir(),exist_ok=True)
                        print_status("Report 발행 시작", "info", f"{search_key}")

                        target_lot_id = search_key.split('_')[0] #{fab_lot_id}
                        target_root_lot_id = target_lot_id[:5] #{root_lot_id}
                        target_DC_step_id = search_key.split('_')[1] #{DC_step_id}
                        target_DC_step = GLOBAL_CONFIG.get("dc_dict").get(target_DC_step_id) #{DC_step}
                        target_step_merged = (target_DC_step or target_DC_step_id) + "(" + target_DC_step_id + ")" #{DC_step_id}({DC_step})

                        match_key = target_root_lot_id + "_" + target_DC_step_id #match_key = {root_lot_id}_{DC_step_id}
                        # 리포트 키 = {fab_lot_id}_{step_id}(원본 키) — anomaly_basis/ai_input/
                        # rule_check/ARCHIVE 산출물 파일명이 전부 이 키를 공유(step별 덮어쓰기 방지)
                        report_key = f"{target_lot_id}_{target_DC_step_id}"
                        if trigger_mode == 'ALL':
                            _publish_all_trends(merged_df, reformatter, vehicle, target_lot_id,
                                                target_root_lot_id, target_DC_step, target_DC_step_id,
                                                trigger_mail, upload_date)
                            continue


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
                        _RUN.stage('inline_query')
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
                        # v9.3.x: 모든 index에 값이 없는 wafer 열 제거 (PPT Score Board)
                        VIP_group_lw = VIP_group_lw.dropna(axis=1, how='all')

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
                        _RUN.stage('chart_render')
                        prs_low_qual, metrics_dict, item_slide_map = insert_plots(merged_df, prs_low_qual, description_image_info_dict_low_qual, target_lot_id, target_root_lot_id, target_DC_step, target_DC_step_id, spec_data, img_quality = 12, ref=False, reformatter=reformatter, dpi=GLOBAL_CONFIG.ppt_chart_dpi)

                        # Score Board Item명 → 해당 차트 슬라이드 내부 하이퍼링크 연결(차트 슬라이드 생성 후)
                        link_scoreboard_items(_sb_item_cells, item_slide_map)

                        _RUN.stage('analysis')
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
                            _RUN.stage('ppt_save')
                            atomic_output(f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}', prs_low_qual.save)
                            print('[INFO]..저장 완료..\n')
                        except PermissionError:
                            raise
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

                        # Inline Table pivot 생성 — 데이터/설정이 없으면 열만 있는 빈 표를 돌려주므로
                        # (예외 없음) 리포트 발행·메일 발송은 그대로 진행된다.
                        inlinedata_filtered_pivot = _build_inline_pivot(
                            inlinedata, inline_file_path, inline_file_sheet, vehicle)


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
                        # v9.3.x: 모든 index에 값이 없는 wafer 열 제거 — 측정 데이터가 전혀 없는
                        #   wafer는 회색 빈 열만 차지하므로 가독성을 위해 열 자체를 숨긴다.
                        VIP_group_HTML = VIP_group_HTML.dropna(axis=1, how='all')
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
                                code_summary_html = render_findings_html(code_findings, top_n=5)
                        except Exception as ce:
                            print(f"[WARN] findings 렌더 스킵 (오류): {ce}")

                        # 2) Anomaly Trend chart 항목 선정 — '통계 기반 자동 분석'(code_findings) 상위와 동일.
                        #    findings(severity 정렬)에서 항목을 순서대로 추출(콤마 분해·중복 제거),
                        #    차트 가능한(merged_df 컬럼 + Trend PNG 존재) 항목만 최대 _top_n개.
                        #    findings로 부족하면 metrics 우선순위(spec_out→deviation)로 보충.
                        def _has_png(_it):
                            _safe = re.sub(r'[\\/:*?"<>|]', '_', str(_it))
                            return os.path.exists(os.path.join(report_temp_dir(),f'{_safe}.png'))
                        # anomaly_exclude_items(+WF MAP 제외 키워드) → 이상/주의 차트에서 완전히 제외
                        _excl_items = list(getattr(GLOBAL_CONFIG, 'anomaly_exclude_items', []) or [])
                        _excl_items += [f"*{str(_k).strip()}*"
                                        for _k in (getattr(GLOBAL_CONFIG, 'wfmap_exclude_keywords', []) or [])
                                        if str(_k).strip()]
                        # 조건부 제외(anomaly_exclude_unless_rule): RULE에 걸려 code_findings에 살아남은
                        #   항목만 Trend chart에 노출하고, 그 외(미매칭)는 metrics 폴백에서도 제외한다.
                        #   (anomaly_engine이 built-in finding을 이미 억제 → code_findings에 없으면 미매칭.)
                        _excl_unless = list(getattr(GLOBAL_CONFIG, 'anomaly_exclude_unless_rule', []) or [])
                        _finding_item_set = set()
                        if _excl_unless:
                            for _f in (code_findings or []):
                                for _fi in str(_f.get('item', '')).split(','):
                                    _fi = _fi.strip()
                                    if _fi:
                                        _finding_item_set.add(_fi)

                        def _is_excluded(_it):
                            if item_excluded(_it, _excl_items):
                                return True
                            # 조건부 제외: RULE 미매칭(=code_findings에 없음)일 때만 제외
                            if (_excl_unless and item_excluded(_it, _excl_unless)
                                    and _it not in _finding_item_set):
                                return True
                            return False
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
                                    or _is_excluded(_it):
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
                                        or _is_excluded(_it):
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
                                    # spec-out WF MAP 데이터 스코프 = '현재 리포트 제품(main vehicle)'만.
                                    #   merged_df에는 with_vehicle(비교용 다른 제품/vehicle)이 concat되어 있어
                                    #   MASK==main_vehicle로 anomaly 분석 모집단(anomaly_engine)과 동일 제품으로 한정한다.
                                    #   단, step은 필터하지 않는다 — Trend chart와 동일하게 '모든 step'의 spec-out
                                    #   wafer가 WF MAP에 나오도록(라벨의 (XX)로 step 구분). render_specout_wfmaps_b64
                                    #   가 각 wafer의 STEP_ID를 라벨에 표기하므로 타 step wafer도 식별 가능하다.
                                    _wfmap_src = merged_df
                                    _wf_mask_col = next((c for c in ('MASK', 'mask')
                                                         if c in _wfmap_src.columns), None)
                                    if _wf_mask_col is not None:
                                        _wf_mv = _wfmap_src[_wfmap_src[_wf_mask_col] == vehicle]
                                        if not _wf_mv.empty:
                                            _wfmap_src = _wf_mv

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

                                    _spec_rows, _warn_items = [], []
                                    _trend_mail_w = max(320, int(GLOBAL_CONFIG.get(
                                        'anomaly_trend_mail_width_px', 460) or 460))
                                    for item in top_item_names:
                                        safe_item = re.sub(r'[\\/:*?"<>|]', '_', str(item))
                                        img_path = os.path.join(report_temp_dir(), f'{safe_item}.png')
                                        if not os.path.exists(img_path):
                                            continue
                                        with open(img_path, "rb") as f:
                                            img_b64 = _img_datauri(f.read())   # 상한 이하 인라인 data URI(첨부 분리 방지)
                                        _tw, _th = _img_px(img_path, _trend_mail_w)
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
                                                # target 판정 = 리포트의 lot_id + step_id 조합.
                                                #   target_step을 빼면 같은 lot의 다른 step WF MAP까지
                                                #   파란 테두리로 묶여 대상 step을 가린다.
                                                _wfmaps = render_specout_wfmaps_b64(
                                                    _wfmap_src, item, spec_low=_slow, spec_high=_shigh,
                                                    target_lot=target_lot_id,
                                                    target_step=target_DC_step_id, max_maps=_wf_max,
                                                    main_vehicle=vehicle)
                                                if _wfmaps:
                                                    # PIL 합성: '해당 lot(target)' WF MAP은 왼쪽에 파란 테두리 블록으로
                                                    # 묶어 표시하고, 오른쪽에 나머지 lot(tkout_time 최신순) 그리드를
                                                    # 이어붙여 1장으로 만든다. 표시 크기 안정화를 위해 합성 캔버스는
                                                    # 항상 2행 높이를 유지하되, 파란 테두리는 실제 target 셀만 감싼다.
                                                    from PIL import Image as _PILImg2, ImageDraw as _PILDraw2, ImageFont as _PILFont2
                                                    import io as _io2
                                                    _map_base = int(GLOBAL_CONFIG.get('anomaly_wfmap_map_size_px', 76) or 76)
                                                    _label_base = int(GLOBAL_CONFIG.get('anomaly_wfmap_label_font_px', 18) or 18)
                                                    _lab_base = int(GLOBAL_CONFIG.get('anomaly_wfmap_label_height_px', 48) or 48)
                                                    _map_sz = max(40, _map_base) * _hs   # config 표시 px × supersample
                                                    _lab_h = max(_label_base + 2, _lab_base) * _hs
                                                    _pad = 3 * _hs       # 셀 간격
                                                    _cell_w = _map_sz + _pad
                                                    _cell_h = _map_sz + _lab_h + _pad
                                                    _wf_tgt = [w for w in _wfmaps if (len(w) > 2 and w[2])]
                                                    _wf_rest = [w for w in _wfmaps if not (len(w) > 2 and w[2])]
                                                    _bpad = 4 * _hs      # 파란 테두리와 맵 사이 여백
                                                    _bw2 = max(2, _hs)   # 파란 테두리 두께(px)
                                                    _gap = 7 * _hs if (_wf_tgt and _wf_rest) else 0   # 블록 간 간격

                                                    def _grid_dims(_n):
                                                        """WF MAP 수와 무관하게 높이가 고정된 2행 그리드."""
                                                        if _n <= 0:
                                                            return 0, 0, 0, 0
                                                        _nc = max(1, -(-_n // 2))
                                                        # 1개뿐이어도 빈 두 번째 행을 캔버스에 포함한다.
                                                        # 합성 이미지는 아래에서 Trend 높이에 맞춰 표시되므로,
                                                        # 실제 높이가 1행이면 단일 WF MAP만 2배 가까이 확대된다.
                                                        _nr = 2
                                                        return (_nc, _nr,
                                                                (_nc - 1) * _cell_w + _map_sz,
                                                                (_nr - 1) * _cell_h + _map_sz + _lab_h)

                                                    _nc_t, _nr_t, _w_t, _h_t = _grid_dims(len(_wf_tgt))
                                                    _nc_r, _nr_r, _w_r, _h_r = _grid_dims(len(_wf_rest))
                                                    # 캔버스용 _h_t는 빈 둘째 행까지 포함하지만 target이 1개면
                                                    # 파란 테두리까지 2행 높이로 늘리지 않는다.
                                                    _nr_t_used = (-(-len(_wf_tgt) // _nc_t)) if _nc_t else 0
                                                    _h_t_box = (((_nr_t_used - 1) * _cell_h + _map_sz + _lab_h)
                                                                if _nr_t_used else 0)
                                                    # 두 블록의 맵 상단은 같은 높이로 정렬(테두리 여백은 target 블록만)
                                                    _y0 = _pad + (_bpad if _wf_tgt else 0)
                                                    _x_t = _pad + (_bpad if _wf_tgt else 0)
                                                    _x_r = (_x_t + _w_t + _bpad + _gap) if _wf_tgt else _pad
                                                    _cw_total = ((_x_r + _w_r + _pad) if _wf_rest
                                                                 else (_x_t + _w_t + _bpad + _pad))
                                                    _ch_total = max(_y0 + _h_t + (_bpad if _wf_tgt else 0),
                                                                    _y0 + _h_r) + _pad
                                                    # 폰트 — 실행 환경에서 파일명을 못 찾으면 PIL 기본 폰트로
                                                    # 떨어져 설정한 px와 무관하게 매우 작아진다. Windows/Linux의
                                                    # 실제 한글 폰트 경로를 순서대로 시도하고, 마지막 기본 폰트도
                                                    # 지원되는 Pillow에서는 요청 크기로 로드한다.
                                                    def _load_wf_label_font(_px):
                                                        _configured = str(GLOBAL_CONFIG.get(
                                                            'anomaly_wfmap_label_font_path', '') or '').strip()
                                                        _font_candidates = [
                                                            _configured,
                                                            'NanumGothic.ttf',
                                                            r'C:\Windows\Fonts\malgun.ttf',
                                                            r'C:\Windows\Fonts\arial.ttf',
                                                            '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
                                                            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                                                            'DejaVuSans.ttf',
                                                        ]
                                                        for _cf in _font_candidates:
                                                            if not _cf:
                                                                continue
                                                            try:
                                                                return _PILFont2.truetype(_cf, _px)
                                                            except Exception:
                                                                continue
                                                        try:
                                                            return _PILFont2.load_default(size=_px)
                                                        except TypeError:
                                                            return _PILFont2.load_default()

                                                    _cfont = _load_wf_label_font(max(12, _label_base * _hs))
                                                    _comp = _PILImg2.new('RGB', (_cw_total, _ch_total), (255, 255, 255))
                                                    _cdraw = _PILDraw2.Draw(_comp)

                                                    def _draw_wf_cell(_wf, _ox, _oy):
                                                        """WF MAP 1셀(맵+라벨) 드로잉 — target lot 라벨은 파란색(bold 아님).

                                                        라벨은 target/그 외 모두 일반(regular) 폰트로 그린다. 구분은
                                                        색(파랑/회색) + target 블록의 파란 테두리로만 한다 — bold는
                                                        같은 폭 셀에서 글자를 굵고 넓게 만들어 가독성이 떨어졌다.
                                                        """
                                                        _lab, _b = str(_wf[0]), _wf[1]
                                                        _is_tgt = _wf[2] if len(_wf) > 2 else False
                                                        _wf_img = _PILImg2.open(_io2.BytesIO(base64.b64decode(_b)))
                                                        _wf_img = _wf_img.resize((_map_sz, _map_sz), _PILImg2.LANCZOS)
                                                        _comp.paste(_wf_img, (_ox, _oy))
                                                        _lcolor = (0, 51, 204) if _is_tgt else (85, 85, 85)
                                                        _lfont = _cfont
                                                        # 라벨 자간(letter-spacing) 조절 — step 접미사 "(XX)"가 붙어
                                                        # 라벨이 맵 셀 폭보다 넓어지면 이웃 라벨과 겹친다. 문자별로
                                                        # 그려 자간을 줄여 셀 pitch(_cell_w) 안에 맞춘다: 기본은
                                                        # 소폭만 좁히고, 넘칠 땐 겹치지 않을 만큼 더 좁힌다.
                                                        def _adv(_ch):
                                                            try:
                                                                return _cdraw.textlength(_ch, font=_lfont)
                                                            except Exception:
                                                                return 6 * _hs
                                                        # lot_id와 #wafer(step)를 2줄로 그린다. 한 줄에 모두
                                                        # 욱여넣던 방식보다 글자 크기를 유지하면서 셀 간 겹침을 막는다.
                                                        _lines = _lab.splitlines() or [_lab]
                                                        _line_h = max(_label_base + 1, 13) * _hs
                                                        for _li, _line in enumerate(_lines[:2]):
                                                            _advs = [_adv(_c) for _c in _line]
                                                            _sum = sum(_advs)
                                                            _n = len(_line)
                                                            _avail = _cell_w - 2 * _hs
                                                            _trk = -0.25 * _hs
                                                            if _n > 1:
                                                                _trk = min(_trk, (_avail - _sum) / (_n - 1))
                                                            _total = _sum + max(0, _n - 1) * _trk
                                                            _ly = _oy + _map_sz + _li * _line_h
                                                            _cx = _ox + (_map_sz - _total) / 2.0
                                                            for _ci, _ch in enumerate(_line):
                                                                _cdraw.text((_cx, _ly), _ch, fill=_lcolor, font=_lfont)
                                                                _cx += _advs[_ci] + _trk

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
                                                             _x_t + _w_t + _bpad - 1,
                                                             _y0 + _h_t_box + _bpad - 1],
                                                            outline=(0, 51, 204), width=_bw2)
                                                    # root lot/wafer 라벨을 포함한 WF MAP 전체 높이를 Trend와
                                                    # 정확히 맞춘다. 종횡비를 유지해 맵/글씨가 찌그러지지 않는다.
                                                    _natural_w = max(1, _cw_total // _hs)
                                                    _natural_h = max(1, _ch_total // _hs)
                                                    _disp_h = max(1, _th)
                                                    _disp_w = max(1, round(_natural_w * _disp_h / _natural_h))
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
                                    _warn_col_w = _trend_mail_w
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

                        ai_html = None

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
                        _lg_ratio = GLOBAL_CONFIG.get('anomaly_lot_dispersion_ratio', 2.0)
                        _lg_fls = float(GLOBAL_CONFIG.get('anomaly_flier_sigma', 3.5) or 0)
                        _lg_flm = int(GLOBAL_CONFIG.get('anomaly_flier_max_pts', 0) or 0)
                        _lg_fodr = float(GLOBAL_CONFIG.get('anomaly_flier_offdir_relax', 2.0) or 1.0)
                        _lg_dgf = float(GLOBAL_CONFIG.get('anomaly_disp_min_spec_frac', 0.0) or 0.0)
                        _lg_agg = ', '.join(f'{k}={v}' for k, v in
                                            (GLOBAL_CONFIG.get('trend_tkout_agg', {}) or {}).items())
                        _lg_agg_txt = (f' (집계 항목 {_lg_agg} 은 site가 아닌 집계값 기준)'
                                       if _lg_agg else '')
                        _lg_fcnt_txt = '1개 이상' if _lg_flm <= 0 else f'1~{_lg_flm}개'
                        _lg_dir_txt = (
                            f' (REPORT DIRECTION=UPPER/LOWER: spec 방향은 정상 감도, 반대 방향은 {_lg_fodr:g}배 완화'
                            f' / BOTH: 양방향 동일 감도)' if _lg_fodr != 1.0
                            else ' (REPORT DIRECTION 방향 완화 없음)')
                        _lg_flier_txt = (
                            f'① Flier — wafer median 대비 |값−median|이 보통 wafer 산포의 '
                            f'{_lg_fls:g}σ를 넘는 pt가 {_lg_fcnt_txt} wafer 존재{_lg_dir_txt}'
                            if _lg_fls > 0 else '① Flier — OFF')
                        _lg_gate_txt = (f'(절대 산포가 spec 폭의 {_lg_dgf * 100:g}% 이상일 때)'
                                        if _lg_dgf > 0 else '')
                        _lg_profile = str(GLOBAL_CONFIG.get('anomaly_detector_profile', 'legacy') or 'legacy')
                        _lg_profiles = GLOBAL_CONFIG.get('anomaly_detector_profiles', {}) or {}
                        _lg_enabled = GLOBAL_CONFIG.get('anomaly_enabled_detectors', None)
                        if _lg_enabled is None:
                            _lg_enabled = _lg_profiles.get(_lg_profile, ['spec_out', 'flier', 'dispersion'])
                        if isinstance(_lg_enabled, str):
                            _lg_enabled = [x.strip() for x in _lg_enabled.split(',') if x.strip()]
                        _lg_enabled = {str(x).lower() for x in (_lg_enabled or [])}
                        _lg_sensitive = _lg_profile.lower() == 'sensitive'
                        _lg_spec_txt = ('해당 lot 측정값 중 spec 이탈 pt가 1개 이상'
                                        if 'spec_out' in _lg_enabled else 'OFF')
                        _lg_flier_txt = (
                            f'① Flier — wafer median 대비 |값−median|이 보통 wafer 산포의 '
                            f'{_lg_fls:g}σ를 넘는 pt가 {_lg_fcnt_txt} wafer 존재{_lg_dir_txt}'
                            if _lg_fls > 0 and 'flier' in _lg_enabled else '① Flier — OFF')
                        _lg_disp_txt = (
                            f'② 산포 확대 — 특정 wafer의 내부 산포가 보통 wafer 산포의 '
                            f'{_lg_ratio:g}배 초과{_lg_gate_txt}'
                            if 'dispersion' in _lg_enabled else '② 산포 확대 — OFF')

                        def _lg_cfg(_key, _default):
                            if _lg_sensitive:
                                _sv = GLOBAL_CONFIG.get(_key + '_sensitive', None)
                                if _sv is not None:
                                    return _sv
                            return GLOBAL_CONFIG.get(_key, _default)

                        _lg_series = []
                        if 'level_shift' in _lg_enabled:
                            _lg_series.append(
                                f'수준 이동 {_lg_cfg("anomaly_level_shift_sigma", 3.0):g}σ 이상')
                        if 'trend' in _lg_enabled:
                            _lg_series.append(
                                f'지속 trend 최근 {int(GLOBAL_CONFIG.get("anomaly_trend_window", 12) or 12)}점·'
                                f'총 변화 {_lg_cfg("anomaly_trend_total_sigma", 3.0):g}σ 이상')
                        if 'spc_run' in _lg_enabled:
                            _lg_series.append(
                                f'SPC 같은 쪽 {int(_lg_cfg("anomaly_spc_same_side_points", 8) or 8)}점 / '
                                '2-of-3 / 4-of-5')
                        _lg_series_txt = ('<br>&nbsp;· <b>시계열 판정</b> : ' + ' · '.join(_lg_series)
                                          if _lg_series else '')
                        _chart_logic = (
                            '<div style="font-size:11px; color:#555555; background:#f7f8fa; '
                            'border:1px solid #e3e6ea; border-radius:4px; padding:6px 10px; '
                            'margin:4px 0 8px 0; line-height:1.7; text-align:left;">'
                            '<b style="color:#003366;">판정 기준</b><br>'
                            f'&nbsp;· <span style="background:#d32f2f; color:#ffffff; font-weight:bold; '
                            f'padding:0 5px; border-radius:2px;">SPEC OUT</span> : '
                            f'{_lg_spec_txt}{_lg_agg_txt}<br>'
                            f'&nbsp;· <span style="background:#f9a825; color:#1a1a1a; font-weight:bold; '
                            f'padding:0 5px; border-radius:2px;">WARNING</span> : 설정된 spec 이탈은 없으나 '
                            f'{_lg_flier_txt} · {_lg_disp_txt}{_lg_series_txt}<br>'
                            f'&nbsp;· <b>SPEC OUT WF MAP</b> : <span style="color:#0033cc; font-weight:bold;">파란 '
                            f'테두리 박스(파란 라벨) = 해당 측정 lot_id({target_lot_id}) + '
                            f'step({target_DC_step_id})내 wafer</span>, '
                            '<span style="color:#555555;">회색 라벨(테두리 없음) = 그 외 tkout_time 기준 최근 '
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
                            raise ValueError('HTML 인라인 이미지 불변식 위반')
                            print(f"[ERROR] HTML 인라인 이미지 불변식 위반 — data:image가 아닌 <img> src "
                                  f"{len(_bad_srcs)}개 발견 (이미지가 깨져 보일 수 있음): "
                                  f"{[s[:60] for s in _bad_srcs[:3]]}")
                        else:
                            print(f"[INFO] HTML 인라인 이미지 검증 OK — <img> {len(_img_srcs)}개 모두 data:image 인라인")

                        # ==================== HTML 저장 ====================
                        _RUN.stage('html_save')
                        atomic_bytes(f'{html_save_path}{fname}', html_content.encode('utf-8'))
                        _capture_artifacts(f'{html_save_path}{fname}', f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}')

                        # ==================== 고화질 PPT(EDM) 미사용 ====================

                        # ==================== S3 업로드 (사내 환경 전용) ====================
                        # My_config.use_s3_upload 로 on/off.
                        _RUN.stage('s3_upload')
                        if not _use_s3:
                            _RUN.current['upload']='disabled'
                            print_status("S3 업로드", "off", f"{search_key} → 기본 AUTO 외 발행 또는 업로드 비활성 설정")
                        elif S3_CONNECT and client:
                            # 개인 이름 경로 없이 bucket_dx 기준 clean key(vehicle/파일명) 사용
                            s3_key = f'{vehicle}/{final_ppt_file_name_DX}'
                            _s3_local = f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}'
                            try:
                                client.upload_file(_s3_local, bucket_dx, s3_key)
                                _RUN.current["upload"]="success"
                                print_status("S3 업로드", "ok", f"{bucket_dx}/{s3_key}")
                            except Exception as s3e:
                                _RUN.current["upload"]="failed"
                                _RUN.data["issues"].append(f"S3 업로드 실패: {search_key}")
                                print_status("S3 업로드", "fail", f"{search_key}: {s3e}")
                        else:
                            _RUN.current["upload"]="unavailable"
                            print_status("S3 업로드", "off", f"{search_key} → S3 미연결 스킵")

                        _RUN.stage('email')
                        _state = _send_report_files(_RUN.current['paths']['html'], _RUN.current['paths']['ppt'],
                                                  email_receiver, f'[HOL] {vehicle} {target_lot_id} {target_step_merged} HOL AUTO REPORT',
                                                  _RUN.current['id'])
                        _RUN.current['email']=_state
                        _RUN.save_report()
                        if _state in ('failed','unknown'):
                            raise RuntimeError(f'메일 발송 {_state}: 운영 이력에서 수신처별 결과 확인 필요')

                        log_to_file(f"{search_key} Report 발행 완료", query_log)
                        _RUN.finish_report('success')
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
                        _RUN.finish_report('failed', str(e))
                        print_status("Report 발행 실패", "fail", f"{search_key}: {e}")
                        traceback.print_exc()
                        log_to_file(f"{search_key} Report 발행 실패: {e}", error_log)
                        continue

                    finally:
                        if _RUN.current:
                            _RUN.finish_report('skipped','대상 step의 측정 데이터 없음 또는 PCHK만 존재')
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
            if not trigger_flag:
                _maybe_send_rule_digest(_json_rules, _LLM_FN)
        except Exception as _dge:
            print(f"[WARN] 규칙 다이제스트 스킵 (오류): {_dge}")

        shutdown_chart_pool()   # 병렬 렌더링 워커 풀 정리 (atexit에도 등록되어 있으나 명시 종료)
        print(f'[INFO] ============== {vehicle} 전체 프로세스 완료 ==============')

    else:
        raise ValueError("reformatter 검증 실패")



def _trend_legend_info(entry, settings):
    """Rank observed groups; proportions are counts of displayed observations."""
    points=entry['points']
    if points.empty:return []
    valid=points.dropna(subset=['_time']).copy()
    if valid.empty:return []
    keys=['_vehicle','_knob'] if '_vehicle' in valid else '_knob'
    colors=['#1685ff','#ff8a00','#00b86b','#ee4266','#9560ff','#00bcd4','#d9ad00','#f04fc4','#38aee8','#83bd00','#ff6540','#b550e8']
    if settings.get('service')=='mlmode':
        from matplotlib import colormaps,colors as mpl_colors
        group_count=valid.groupby(keys,sort=True).ngroups
        palette=colormaps['tab20'] if group_count<=20 else colormaps['hsv'].resampled(group_count+1)
        colors=[mpl_colors.to_hex(palette(i)) for i in range(group_count)]
    # Calendar recent material is independent of publication highlight and process x axis.
    clock='_dc_time' if '_dc_time' in valid else '_time'
    end=pd.Timestamp(settings.get('report_now',valid[clock].max()))
    rows=[]
    for i,(key,g) in enumerate(valid.groupby(keys,sort=True)):
        knob=key[-1] if isinstance(key,tuple) else key
        rows.append(dict(key=key,label=' / '.join(map(str,key)) if isinstance(key,tuple) else str(key),
            color='#999999' if 'UNMATCHED' in str(knob) else ('#245886' if knob=='ALL' else colors[i%len(colors)]),
            count=len(g),highlight=int(g['_recent'].sum()) if '_recent' in g else 0,
            fortnight=int(g[clock].between(end-pd.Timedelta(days=14),end).sum())))
    overall=max(rows,key=lambda r:r['count']);recent=max(rows,key=lambda r:r['fortnight'])
    highlighted=sorted([r for r in rows if r['highlight']],key=lambda r:(-r['highlight'],-r['count'],r['label']))
    ordered=[]
    # Reserve space for both dominant groups even if highlighted groups exceed the HTML cap.
    cap=max(2,int(settings.get('html_legend_limit',10)))
    for r in highlighted[:max(0,cap-2)]+[overall,recent]+highlighted+sorted(rows,key=lambda r:(-r['count'],-r['fortnight'],r['label'])):
        if r not in ordered:ordered.append(r)
    return ordered


def _service_html_start(title, purpose, stamp=''):
    """Shared, mail-safe shell matching the production Auto Report template."""
    import html
    esc=lambda value:html.escape(str(value))
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+
            '</title></head><body style="margin:0;background:#ffffff">'
            '<div id="top" style="font-family:Segoe UI,Arial,Malgun Gothic,sans-serif;font-size:12px;'
            'color:#1a1a1a;line-height:1.5;padding:16px 24px;background:#ffffff">'
            '<h1 style="color:#003366;font-size:20px;border-bottom:2px solid #003366;'
            'padding-bottom:6px;margin:0 0 6px">'+esc(title)+'</h1>'
            '<p style="color:#555;font-size:13px;margin:4px 0">'+esc(purpose)+'</p>'
            '<p style="color:#555;margin:4px 0 14px">'+esc(stamp)+'</p>')


def _service_heading(title, anchor=''):
    import html
    return ('<h2 id="'+html.escape(anchor,quote=True)+'" style="border-left:4px solid #003366;'
            'padding:4px 8px;font-size:15px;color:#003366;margin:20px 0 8px">'+html.escape(title)+'</h2>')


def _service_table(headers, rows):
    import html
    esc=lambda value:html.escape(str(value))
    body='<table cellpadding="5" cellspacing="0" width="100%" style="border-collapse:collapse;font-size:12px;line-height:1.5"><thead><tr>'
    body+=''.join('<th scope="col" style="background:#e8edf3;color:#003366;text-align:left;border:1px solid #cbd5df;padding:6px">'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'
    for i,row in enumerate(rows):
        body+='<tr>'+''.join('<td style="vertical-align:top;overflow-wrap:anywhere;border:1px solid #dce2e8;padding:6px;background:'+('#fafbfc' if i%2 else '#ffffff')+'">'+esc(v)+'</td>' for v in row)+'</tr>'
    if not rows:body+='<tr><td colspan="'+str(len(headers))+'" style="padding:10px;border:1px solid #dce2e8;color:#666">해당 이력 없음</td></tr>'
    return body+'</tbody></table>'


def _service_metrics(metrics):
    import html
    return ('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:12px 0;border-top:1px solid #cbd5df;border-bottom:1px solid #cbd5df"><tr>'+
            ''.join('<td style="padding:10px 12px;background:#f4f7fa;vertical-align:top"><span style="font-size:12px;color:#555">'+html.escape(str(label))+'</span><br><strong style="font-size:22px;color:'+color+'">'+html.escape(str(value))+'</strong></td>' for label,value,color in metrics)+'</tr></table>')


def _trend_review(entry, ml=False):
    """A review label is not a pass/fail decision; missing evidence stays visible."""
    if not entry.get('n'):return '자료 없음','#666666','측정 DB와 항목 매핑을 확인하세요.'
    if ml:
        if entry.get('ml_findings'):return '검토 후보','#a45100','탐지 근거와 비교 lot을 확인한 뒤 공정 이력을 대조하세요.'
        if not entry.get('ml_test_count'):return '분석 불가','#666666','과거·신규 lot 수와 시간·Split 매칭을 확인하세요.'
        return '탐지 없음','#1f497d','실행된 검정에서 설정 기준을 만족하는 후보가 없습니다.'
    if (entry.get('recent_out_pct') or 0)>0:return '신규 Spec 이탈','#b4232d','신규·변경 측정의 이탈 lot과 측정 재현성을 확인하세요.'
    if entry.get('signals'):return '변화 확인','#b4232d','Spec 이탈 및 Split별 변화가 최근 lot에서도 이어지는지 확인하세요.'
    if entry.get('out_pct') is None:return 'Spec 없음','#666666','추이 참고용입니다. 제품 reformatter의 Spec을 확인하세요.'
    if entry.get('warnings'):return '자료 확인','#a45100','자료 제한을 확인한 뒤 추이를 해석하세요.'
    return '기준 신호 없음','#1f497d','설정된 반복·변화 기준 미충족입니다. 전체 추이를 함께 확인하세요.'


def _trend_brief(entries, settings):
    """Part-local summary, with whole-publication analysis coverage clearly labelled."""
    import html
    ml=settings.get('service')=='mlmode'
    owners=[e for e in entries if not e.get('parent_item')]
    labels=[_trend_review(e,ml) for e in owners]
    body=_service_metrics([('이번 파일 항목·조건',len(owners),'#003366'),
          ('검토 후보' if ml else '우선 확인 항목',sum(l[0] in ('검토 후보','변화 확인','신규 Spec 이탈') for l in labels),'#b4232d'),
          ('신규 관측 있는 항목',sum(bool(e.get('recent_lots')) for e in owners),'#003366'),
          ('자료 확인 항목',sum(bool(e.get('warnings')) or not e.get('n') for e in owners),'#a45100')])
    body+='<p style="color:#555">집계 단위는 항목 × Step × 프로그램 × 온도입니다. 같은 lot이 여러 항목에 포함되므로 lot 수를 합산하지 않습니다.</p>'
    coverage=settings.get('_coverage',[])
    if coverage:
        body+=_service_heading('발행 범위 · 전체 발행 기준')+_service_table(['제품','대상 조건 수','자료 상태','제품 설정 기간'],
             [[r['vehicle'],r['items'],{'ok':'자료 있음','no_data':'측정 자료 없음','no_category':'선택 Category 없음'}.get(r['status'],r['status']),
               str(r.get('viewing_period','-'))+'일'] for r in coverage])
    if ml:
        analysis=settings.get('_analysis',{})
        body+='<p>전체 분석: 대상 '+str(analysis.get('tested',len(owners)))+' / 검정 실행 항목 '+str(analysis.get('tested_items',0))+' / 검정 미실행 항목 '+str(analysis.get('untested_items',0))+' / 통계 검정 '+str(analysis.get('statistical_tests',0))+'회. 일부 모듈 제외 사유는 항목별 자료 제한을 확인하세요.</p>'
        body+='<p>탐지 기준: 보정 q ≤ '+html.escape(str(settings.get('fdr_alpha',.05)))+' 및 효과 기준 ≥ '+html.escape(str(settings.get('effect_sigma',1.5)))+'. q는 불량률이 아닙니다. 연관 후보는 원인 확정이나 공정 변경 지시가 아닙니다.</p>'
    else:
        body+='<p>검정 테두리 점은 이전 성공 발행 이후 신규·변경 관측이며 첫 발행은 당일 측정입니다. Spec out은 표시 기간의 전체 유효 값 기준이며, 집계 항목은 설정된 집계값으로 계산합니다.</p>'
    ranked=sorted(owners,key=lambda e:(0 if _trend_review(e,ml)[0] in ('검토 후보','변화 확인','신규 Spec 이탈') else 1,0 if e.get('warnings') or not e.get('n') else 1,e['vehicle'],e['category'],e['item']))
    limit=max(1,min(30,int(settings.get('summary_max_items',12))))
    rows=[]
    for e in ranked[:limit]:
        state,_,action=_trend_review(e,ml)
        evidence=_ml_finding_summary(e) if ml else e.get('reason','')
        rows.append([e['vehicle']+' / '+e['category'],e['item']+' / '+e['step']+' / '+e['program']+' / '+str(e['temperature']),state,
                     str(e['lots'])+' / '+str(e.get('recent_lots',0)),evidence,action])
    body+=_service_heading('먼저 확인할 항목')+_service_table(['제품 / Category','항목 / 측정 조건','확인 구분','전체 / 신규 lot','근거','다음 확인'],rows)
    if len(ranked)>limit:body+='<p>요약은 '+str(limit)+'개 항목·조건입니다. 전체 '+str(len(ranked))+'개 차트와 자료 제한은 아래 본문 및 catalog.csv에서 확인하세요.</p>'
    return body


def _service_deck_style(prs):
    """Apply the production title/font theme only to independent service decks."""
    from pptx.dml.color import RGBColor
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:continue
            for p in shape.text_frame.paragraphs:
                p.font.name=getattr(GLOBAL_CONFIG,'theme_font_family','Malgun Gothic')
                if shape.top<500000:p.font.color.rgb=RGBColor(*getattr(GLOBAL_CONFIG,'theme_title_color',(31,73,125)))


def _daily_trend_chart(entry, settings):
    """Daily compact trend or taller ML trend; legends for ML live outside the plot."""
    import io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=(7.2,3.2 if settings.get('service')=='mlmode' else 2.65))
    try:
        points=entry['points']
        if points.empty:
            ax.text(.5,.5,entry['reason'],ha='center',va='center',transform=ax.transAxes)
            ax.set_axis_off()
        else:
            valid=points.dropna(subset=['_time'])
            legend_rows=_trend_legend_info(entry,settings)
            entry['_legend_rows']=legend_rows
            group_keys=['_vehicle','_knob'] if '_vehicle' in valid else '_knob'
            color_lookup={r['key']:r['color'] for r in legend_rows}
            for key,group in valid.groupby(group_keys,sort=True):
                label=' / '.join(map(str,key)) if isinstance(key,tuple) else str(key)
                color=color_lookup[key]
                recent=group['_recent'] if '_recent' in group else pd.Series(False,index=group.index)
                background=group.loc[~recent];highlight=group.loc[recent]
                ax.scatter(background['_time'],background['_value'],s=float(settings.get('trend_marker_size',18)),alpha=float(settings.get('trend_background_alpha',.3)),color=color,edgecolors='none',antialiaseds=False,label=str(label))
                ax.scatter(highlight['_time'],highlight['_value'],s=float(settings.get('trend_recent_marker_size',32)),alpha=1,color=color,
                           edgecolors='black',linewidths=.7,antialiaseds=False,zorder=4)
            if valid.empty:ax.text(.5,.5,'No matched process timestamps',ha='center',transform=ax.transAxes)
            locator=mdates.AutoDateLocator(minticks=3,maxticks=6)
            ax.xaxis.set_major_locator(locator);ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
            if not valid.empty:
                daily=valid.groupby(valid['_time'].dt.floor('D'))['_value'].median().sort_index()
                median=daily.rolling('3D',min_periods=1).mean()
                ax.plot(median.index,median.values,color='black',lw=1.5,zorder=6)
                if not settings.get('_ppt_chart') and settings.get('service')!='mlmode':
                    from matplotlib.lines import Line2D
                    selected=legend_rows[:max(2,int(settings.get('html_legend_limit',10)))]
                    handles=[Line2D([],[],marker='o',ls='',color=r['color'],markeredgecolor='black' if r['highlight'] else r['color'],markersize=5) for r in selected]
                    labels=[r['label'] if len(r['label'])<=32 else r['label'][:29]+'...' for r in selected]
                    handles.append(Line2D([],[],color='black',lw=1.5));labels.append('Daily median: 3D mean')
                    ax.legend(handles,labels,fontsize=9,loc='upper left',ncol=4,framealpha=.95,borderpad=.2,labelspacing=.15,columnspacing=.55,handletextpad=.25,handlelength=1.1,borderaxespad=.25,markerscale=.85)
            if entry['log_scale'] and points['_value'].gt(0).all():
                ax.set_yscale('log')
            for bound in (entry['low'],entry['high']):
                if bound is not None:
                    ax.axhline(bound,color='#c63737',ls='--',lw=.9)
        if not points.empty and not entry['log_scale']:
            focused=points.loc[points['_recent']] if '_recent' in points else points.iloc[:0]
            if not focused.empty:
                values=list(focused['_value'].dropna())+[v for v in (entry['low'],entry['high']) if v is not None]
                daily_values=valid.groupby(valid['_time'].dt.floor('D'))['_value']
                band_pct=float(settings.get('trend_ylim_band_pct',GLOBAL_CONFIG.get('trend_ylim_band_pct',25)) or 25)/100
                band_pct=min(.49,max(0,band_pct))
                for series in (daily_values.quantile(band_pct),daily_values.quantile(1-band_pct),daily_values.median()):
                    values.extend(series.sort_index().rolling('3D',min_periods=1).mean().dropna())
                values=[v for v in values if np.isfinite(v)]
                if values:
                    lo,hi=min(values),max(values);pad=(hi-lo)*.08 if hi>lo else abs(hi)*.08 or 1
                    ax.set_ylim(lo-pad,hi+pad)
                    entry['_focus_ylim']=(lo-pad,hi+pad)
        ax.set_xlabel(entry['x_label'],fontsize=14,labelpad=2);ax.set_ylabel(entry['unit'],fontsize=14,labelpad=2)
        for axis in (ax,):axis.tick_params(labelsize=12,pad=2);axis.grid(alpha=.15)
        fig.tight_layout(pad=.35)
        stream=io.BytesIO();fig.savefig(stream,format='png',dpi=int(settings.get('chart_dpi',150)))
        from PIL import Image
        image=Image.open(io.BytesIO(stream.getvalue())).convert('RGB')
        count=int(settings.get('trend_palette_colors',256 if settings.get('service')=='mlmode' else 64))
        from PIL import ImageColor
        reserved=list(dict.fromkeys(['#ffffff','#000000']+[r['color'] for r in entry.get('_legend_rows',[])]))
        count=max(count,len(reserved)+4)
        adaptive=image.quantize(colors=count-len(reserved),method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
        palette=[]
        for color in reserved:palette.extend(ImageColor.getrgb(color))
        palette+=adaptive.getpalette()[:(count-len(reserved))*3]
        palette+=palette[-3:]*(256-len(palette)//3)
        reference=Image.new('P',(1,1));reference.putpalette(palette)
        quantized=image.quantize(palette=reference,dither=Image.Dither.NONE)
        quantized=quantized.point([min(i,count-1) for i in range(256)])
        quantized.putpalette(palette[:count*3])
        packed=io.BytesIO();quantized.save(packed,format='PNG',optimize=True)
        return packed.getvalue()
    finally:plt.close(fig)


def _ml_spatial_details(entry, settings):
    """Product-separated composites combine splits; radial profiles retain split colors."""
    import io
    import matplotlib.pyplot as plt
    import My_Function as wf
    from matplotlib.colors import Normalize
    source=entry.get('spatial',pd.DataFrame())
    if source.empty or not {'chip_x_pos','chip_y_pos'}.issubset(source):
        entry['warnings'].append('Spatial detail unavailable: shot X/Y missing')
        return []
    source=source.copy()
    if '_vehicle' not in source:source['_vehicle']=entry['vehicle']
    recent_values=source.loc[source['_recent'],'_value']
    value_min=float(recent_values.min()) if len(recent_values) else 0.
    value_max=float(recent_values.max()) if len(recent_values) else 1.
    scale_keys=['_vehicle','_knob','chip_x_pos','chip_y_pos']+(['flat_zone'] if 'flat_zone' in source else [])
    wafer_keys=scale_keys+['fab_lot_id','wafer_id','_recent']
    balanced=source.groupby(wafer_keys,dropna=False)['_value'].median().reset_index()
    contrasts=balanced.groupby(scale_keys+['_recent'],dropna=False)['_value'].median().unstack('_recent')
    delta_span=max(float((contrasts[True]-contrasts[False]).abs().max()),1e-12) if True in contrasts and False in contrasts else 1.
    if not np.isfinite(delta_span):delta_span=1.
    output=[]
    for vehicle,group in source.groupby('_vehicle',dropna=False):
        knob='ALL splits'
        group=group.copy();calibrated=False
        layout=getattr(wf,'_CHIP_LAYOUT',None)
        if layout is not None and {'MASK','CHIP_X_POS','CHIP_Y_POS','CHIP_X_ADJ','CHIP_Y_ADJ'}.issubset(layout):
            chosen=layout.loc[layout.MASK.astype(str).eq(str(vehicle))].copy()
            join={'CHIP_X_POS':'chip_x_pos','CHIP_Y_POS':'chip_y_pos'}
            if 'flat_zone' in group and 'FLAT_ZONE_POS' in chosen:join['FLAT_ZONE_POS']='flat_zone'
            cols=list(join)+['CHIP_X_ADJ','CHIP_Y_ADJ']+(['Chip_Radius'] if 'Chip_Radius' in chosen else [])
            chosen=chosen[cols].rename(columns=join).drop_duplicates()
            if len(chosen) and not chosen.duplicated(list(join.values())).any():
                group=group.merge(chosen,on=list(join.values()),how='left',validate='many_to_one')
                missing=group[['CHIP_X_ADJ','CHIP_Y_ADJ']].isna().any(axis=1)
                if missing.any():entry['warnings'].append(f'Wafer geometry unmatched: {int(missing.sum())} shots excluded from maps')
                group=group.loc[~missing].copy()
                group['chip_x_pos']=group.CHIP_X_ADJ;group['chip_y_pos']=group.CHIP_Y_ADJ
                calibrated=True
        if group.empty:continue
        cx,cy=('CHIP_X_ADJ','CHIP_Y_ADJ') if calibrated else ('chip_x_pos','chip_y_pos')
        geom=group
        circ=wf._wafer_circle_params(geom,cx,cy,'Chip_Radius' if 'Chip_Radius' in geom else None,main_vehicle=vehicle)
        if not calibrated:entry['warnings'].append('Wafer map uses estimated outline: product coordinate layout unavailable')
        pitch=wf._wfmap_shot_pitch_xy(geom,cx,cy,main_vehicle=vehicle)
        limits=wf._wfmap_axis_limits(*wf._wfmap_grid_limits(geom,cx,cy,main_vehicle=vehicle),circ)
        recent=group.loc[group['_recent']].copy();history=group.loc[~group['_recent']].copy()
        if recent.empty:continue
        keys=['fab_lot_id','wafer_id','_knob','chip_x_pos','chip_y_pos']
        if 'flat_zone' in group:keys.append('flat_zone')
        # Repeated measurements and shots from one wafer cannot dominate a composite.
        wafer=recent.groupby(keys,dropna=False)['_value'].median().reset_index()
        baseline=history.groupby(keys,dropna=False)['_value'].median().reset_index()
        for field in ['chip_x_pos','chip_y_pos']:
            wafer[field]=pd.to_numeric(wafer[field],errors='coerce')
            baseline[field]=pd.to_numeric(baseline[field],errors='coerce')
        wafer=wafer.dropna(subset=['chip_x_pos','chip_y_pos'])
        if wafer.empty:continue
        # Flat zones have independent site coordinates; keep them on distinct detail rows.
        zones=wafer.groupby('flat_zone',dropna=False) if 'flat_zone' in wafer else [('ALL',wafer)]
        for zone,w in zones:
            b=baseline.loc[baseline.flat_zone.eq(zone)] if 'flat_zone' in baseline and pd.notna(zone) else baseline
            site=w.groupby(['chip_x_pos','chip_y_pos']).agg(value=('_value','median'),n=('_value','size')).reset_index()
            base=b.groupby(['chip_x_pos','chip_y_pos'])['_value'].median().rename('baseline').reset_index()
            site=site.merge(base,on=['chip_x_pos','chip_y_pos'],how='left',validate='one_to_one')
            site['delta']=site.value-site.baseline
            fig,axes=plt.subplots(1,3,figsize=(12.4,3.6))
            try:
                scatter=wf._draw_wfmap_shots(axes[0],site.chip_x_pos,site.chip_y_pos,*pitch,values=site.value,cmap='viridis',norm=Normalize(value_min,value_max))
                fig.colorbar(scatter,ax=axes[0],pad=.02).ax.tick_params(labelsize=10)
                axes[0].set_title('New observations: site median',fontsize=12)
                valid=site.dropna(subset=['delta'])
                if len(valid):
                    span=delta_span
                    sc=wf._draw_wfmap_shots(axes[1],valid.chip_x_pos,valid.chip_y_pos,*pitch,values=valid.delta,cmap='coolwarm',norm=Normalize(-span,span))
                    fig.colorbar(sc,ax=axes[1],pad=.02).ax.tick_params(labelsize=10)
                else:axes[1].text(.5,.5,'No historical matched sites',ha='center',transform=axes[1].transAxes,fontsize=8)
                axes[1].set_title('New minus historical site median',fontsize=12)
                for ax in axes[:2]:
                    wf._add_wafer_circle(ax,circ,color='#172b43',lw=1.3,zorder=4)
                    ax.set_xlim(limits[0],limits[1]);ax.set_ylim(limits[3],limits[2])
                    ax.set_aspect(wf._wfmap_aspect(circ),adjustable='box')
                    ax.set_xticks([]);ax.set_yticks([])
                    for spine in ax.spines.values():spine.set_visible(False)
                # One split overlay, wafer-balanced median at each observed radius.
                color_rows=_trend_legend_info(entry,settings)
                color_rows=[r for r in color_rows if not isinstance(r['key'],tuple) or str(r['key'][0])==str(vehicle)]
                colors={r['key'][-1] if isinstance(r['key'],tuple) else r['key']:r['color'] for r in color_rows}
                for split,radial in w.groupby('_knob',dropna=False):
                    radial=radial.copy()
                    if calibrated and 'Chip_Radius' in group:
                        lookup=group.groupby(['chip_x_pos','chip_y_pos'])['Chip_Radius'].median()
                        radial['radius']=pd.MultiIndex.from_frame(radial[['chip_x_pos','chip_y_pos']]).map(lookup).to_numpy()
                    else:radial['radius']=np.hypot(radial.chip_x_pos-(circ[0] if circ else 0),radial.chip_y_pos-(circ[1] if circ else 0))
                    per_wafer=radial.dropna(subset=['radius']).groupby(['fab_lot_id','wafer_id','radius'])['_value'].median()
                    profile=per_wafer.groupby('radius').median().sort_index()
                    color=colors.get(split,'#1685ff')
                    axes[2].scatter(profile.index,profile.values,s=18,color=color,label=str(split),zorder=4)
                    if len(profile)>=4:
                        fit=np.polynomial.Polynomial.fit(profile.index.to_numpy(),profile.to_numpy(),3)
                        grid=np.linspace(profile.index.min(),profile.index.max(),100)
                        axes[2].plot(grid,fit(grid),color=color,lw=1.5)
                    else:entry['warnings'].append(f'{split}: cubic fit unavailable; fewer than 4 distinct radii')
                axes[2].set_title('Radius median / cubic fit',fontsize=12)
                axes[2].set_xlabel('Wafer radius (mm)' if calibrated and 'Chip_Radius' in group else 'Radius (uncalibrated grid units)',fontsize=10)
                axes[2].set_ylabel(entry['unit'],fontsize=10)
                for ax in axes:ax.tick_params(labelsize=10)
                axes[2].grid(alpha=.15)
                fig.tight_layout(pad=.8);buf=io.BytesIO();fig.savefig(buf,format='png',dpi=int(settings.get('chart_dpi',150)))
                detail=dict(entry,parent_item=entry['item'],item=entry['item']+' / spatial / '+str(vehicle)+' / '+str(knob)+' / zone '+str(zone),
                            png=buf.getvalue(),reason=f'Wafer-balanced medians; {len(w)} wafer-sites; site support {int(site.n.min())}-{int(site.n.max())}. Delta is not a spec failure.',
                            warnings=list(entry['warnings'])+([] if calibrated else ['Coordinate layout unavailable: estimated wafer outline / grid radius.']),
                            _legend_rows=color_rows)
                output.append(detail)
            finally:plt.close(fig)
    return output


class _DailyTrendLimit(Exception):
    """Planning failed before any report mail was sent."""


def _trend_category_key(entry, settings):
    order=settings.get('category_order') or []
    if isinstance(order,dict):order=order.get(entry['vehicle'],[])
    category=entry['category']
    return (entry['vehicle'],order.index(category) if category in order else len(order),category,entry['item'],entry['step'])


def _ml_reading_note(entry):
    if ' / spatial / ' in entry['item']:
        return ('왼쪽: 신규 관측의 위치별 중앙값(모든 Split 합성). 가운데: 신규 - 과거, 빨강은 증가·파랑은 감소이며 Spec 불량 표시는 아닙니다. '
                '오른쪽: Split별 반경 중앙값(점)과 3차 근사선. 색은 Trend와 같습니다.')
    return ('색: 제품 / Split. 검정 테두리: 이전 성공 발행 이후 신규·변경 관측(첫 발행은 당일 측정). '
            '검정선: 전체 그룹의 일별 중앙값을 3일 이동평균한 참고선. X축: '+str(entry.get('x_label','시간 정보 없음'))+
            '. Y축: '+str(entry.get('unit','단위 정보 없음'))+' / '+str(entry['aggregation'])+'. 항목별 Y축 범위는 다를 수 있습니다.')


def _ml_finding_summary(entry):
    names={'isolation_forest':'Isolation Forest 이상 증가','local_outlier_factor':'주변 패턴 대비 이상 증가',
           'spatial_pattern':'웨이퍼 공간 패턴 변화','time_trend':'시간 추이 변화','split_difference':'Split 간 차이',
           'equipment_difference':'장비 간 차이','spike_rate':'극단값 비율 증가'}
    findings=entry.get('ml_findings',[])
    summary=' · '.join(dict.fromkeys(names.get(f['module'],f['module']) for f in findings)) or entry['reason']
    if findings:summary+=' / 보정 q 최소 '+format(min(f['q'] for f in findings),'.3g')
    return summary


def _ml_candidate_chart(entry, candidate, spatial=False):
    """One candidate/cohort, shared split colors; each dot is an independent root summary."""
    import io
    import matplotlib.pyplot as plt
    colors=['#0072B2','#D55E00','#009E73','#CC79A7']
    frame=pd.DataFrame(candidate['plot']);labels=sorted(frame.x.astype(str).unique()) if candidate['kind']=='categorical' else []
    palette={label:colors[i%len(colors)] for i,label in enumerate(labels)}
    fig,ax=plt.subplots(figsize=(6.2,2.7));plt.rcParams['axes.unicode_minus']=False
    if candidate['kind']=='categorical':
        for i,label in enumerate(labels):
            group=frame.loc[frame.x.astype(str).eq(label)]
            box=ax.boxplot([group.y],positions=[i],widths=.45,patch_artist=True,showfliers=False)
            box['boxes'][0].set_facecolor(palette[label]);box['boxes'][0].set_alpha(.3)
            jitter=np.linspace(-.13,.13,len(group))
            ax.scatter(i+jitter,group.y,s=13,c=palette[label],edgecolors=np.where(group.recent,'black',palette[label]),linewidths=.6,alpha=.8)
        ax.set_xticks(range(len(labels)),[str(v) if len(str(v))<30 else str(v)[:27]+'…' for v in labels])
        if len(labels)>4:
            ax.set_xticks(range(len(labels)),[str(v)[:18] for v in labels],rotation=30,ha='right')
        ax.set_xlabel('Root-lot medians / black edge = new',fontsize=10)
    else:
        ax.scatter(frame.x,frame.y,s=14,c='#0072B2',edgecolors=np.where(frame.recent,'black','#0072B2'),linewidths=.6,alpha=.75)
        ax.set_xlabel(candidate['column'],fontsize=10)
    ax.set_ylabel(entry['item']+' ('+str(entry.get('unit',''))+')',fontsize=10)
    ax.set_title((f"#{candidate['rank']} {candidate['column']} | effect {candidate['effect']:.2f} · q {candidate['q']:.3g}" if candidate['rank'] else 'Split · root medians'),fontsize=11,loc='left')
    ax.grid(axis='y',alpha=.2);ax.spines[['top','right']].set_visible(False);ax.tick_params(labelsize=9)
    fig.tight_layout(pad=.8);stream=io.BytesIO();fig.savefig(stream,format='png',dpi=140);plt.close(fig)
    return stream.getvalue()


def _ml_split_spatial(entry,candidate):
    """Descriptive maps/profile for the tested root cohort; raw spatial values are labeled separately."""
    import io
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    raw=entry.get('spatial',pd.DataFrame());col='__ml_'+candidate['column']
    if candidate['kind']!='categorical' or not {col,'chip_x_pos','chip_y_pos','root_lot_id'}.issubset(raw):return None
    labels=sorted(pd.DataFrame(candidate['plot']).x.astype(str).unique());colors=['#0072B2','#D55E00']
    raw=raw.loc[raw[col].astype(str).isin(labels)].copy()
    if '_vehicle' in raw:raw=raw.loc[raw._vehicle.astype(str).eq(candidate['vehicle'])]
    cohort={(str(p['fab_lot_id']),str(p['x'])) for p in candidate['plot']}
    raw=raw.loc[[(str(r),str(x)) in cohort for r,x in zip(raw.root_lot_id,raw[col])]]
    clock='_dc_time' if '_dc_time' in raw else '_time'
    raw=raw.loc[raw[clock].eq(raw.groupby(['root_lot_id','wafer_id'])[clock].transform('max'))]
    for c in ['chip_x_pos','chip_y_pos','_value']:raw[c]=pd.to_numeric(raw[c],errors='coerce')
    raw=raw.dropna(subset=['chip_x_pos','chip_y_pos','_value'])
    if raw.empty:return None
    if 'flat_zone' in raw and raw.flat_zone.nunique()>1:
        entry['warnings'].append('Split map unavailable: multiple flat zones need separate geometry');return None
    # Reduce repeated shots -> wafer/site -> root/site -> split/site, equal root weight.
    wafer=raw.groupby([col,'root_lot_id','wafer_id','chip_x_pos','chip_y_pos'],dropna=False)._value.median().reset_index()
    roots=wafer.groupby([col,'root_lot_id','chip_x_pos','chip_y_pos'])._value.median().reset_index()
    sites=roots.groupby([col,'chip_x_pos','chip_y_pos'])._value.median().reset_index()
    norm=Normalize(float(sites._value.min()),float(sites._value.max()))
    fig,axes=plt.subplots(1,3,figsize=(12.4,3.0),layout='constrained')
    bound=max(float(np.abs(sites[['chip_x_pos','chip_y_pos']]).max().max()),1)*1.12
    for i,label in enumerate(labels[:2]):
        s=sites.loc[sites[col].astype(str).eq(label)]
        sc=axes[i].scatter(s.chip_x_pos,s.chip_y_pos,c=s._value,cmap='viridis',norm=norm,marker='s',s=85)
        axes[i].set(xlim=(-bound,bound),ylim=(-bound,bound),aspect='equal',xlabel='Shot X',ylabel='Shot Y')
        axes[i].set_title(str(label)[:38],color=colors[i],fontsize=11)
        r=roots.loc[roots[col].astype(str).eq(label)].copy()
        r['radius']=np.hypot(r.chip_x_pos,r.chip_y_pos)
        r['bin']=np.round(r.radius/max(bound/10,1e-9)).astype(int)
        profile=r.groupby(['root_lot_id','bin']).agg(radius=('radius','median'),value=('_value','median')).reset_index()
        profile=profile.groupby('bin').agg(radius=('radius','median'),value=('value','median'))
        axes[2].plot(profile.radius,profile.value,'o-',ms=3,color=colors[i],label=str(label)[:30])
    fig.colorbar(sc,ax=list(axes[:2]),shrink=.78,pad=.02,label='Raw spatial value · shared scale')
    axes[2].set_title('Radius profile · same Split colors',fontsize=11)
    axes[2].set_xlabel('Radius from shot origin (grid units)',fontsize=10);axes[2].set_ylabel('Root-balanced raw median',fontsize=10)
    axes[2].legend(fontsize=8,frameon=False,loc='best');axes[2].grid(alpha=.2)
    for ax in axes:ax.tick_params(labelsize=9)
    stream=io.BytesIO();fig.savefig(stream,format='png',dpi=140);plt.close(fig)
    return stream.getvalue()


def _ml_compact_html(entry, panels, maps, settings):
    """One item = one mail-width evidence sheet; no hidden interaction required."""
    import html
    esc=lambda v:html.escape(str(v))
    candidates=entry.get('_influence',{}).get('candidates',[])
    def picture(png,caption):
        return '<div style="font-size:12px;color:#003366;padding:2px 6px">'+esc(caption)+'</div><img alt="'+esc(caption)+'" style="display:block;width:100%;height:auto" src="'+_img_datauri(png)+'">'
    def empty(caption):
        return '<div style="height:160px;padding:20px;color:#777;font-size:12px">'+esc(caption)+'</div>'
    label=' / '.join(str(entry.get(k,'')) for k in ('vehicle','category','item','step','program','temperature'))
    content='<section class="ml-item" style="max-width:1320px;margin:14px auto 24px;border:1px solid #cbd5df;background:white">'
    content+='<h2 style="font-size:16px;color:#003366;background:#e8edf3;padding:6px 10px;margin:0">'+esc(label)+'</h2>'
    content+='<div style="font-size:12px;padding:5px 10px">'+esc(_ml_finding_summary(entry))+'</div>'
    legend=entry.get('_legend_rows',[])
    if legend:
        content+='<div style="font-size:10px;padding:2px 10px">'+''.join('<span style="display:inline-block;margin-right:10px"><span style="color:'+r['color']+'">●</span> '+esc(r['label'])+'</span>' for r in legend)+'</div>'
    if candidates:
        content+='<table width="100%" cellspacing="0" cellpadding="3" style="font-size:11px;border-collapse:collapse"><tr style="color:#555;background:#f4f7fa"><th>Rank</th><th>Product</th><th>Factor</th><th>Comparison</th><th>Effect</th><th>q</th><th>Roots</th></tr>'
        for c in candidates:
            content+='<tr>'+''.join('<td style="text-align:center;border-bottom:1px solid #eee">'+esc(v)+'</td>' for v in [c['rank'],c.get('vehicle',''),c['column'],c['comparison'],f"{c['effect']:.2f}",f"{c['q']:.2g}",c['n']])+'</tr>'
        content+='</table>'
    cat=next((i for i,c in enumerate(candidates) if c['kind']=='categorical'),None)
    num=next((i for i,c in enumerate(candidates) if c['kind']=='numeric'),None)
    cells=[picture(entry['png'],'Trend'),
           picture(panels[cat],'Box · #'+str(candidates[cat]['rank'])) if cat is not None else _ml_context_box(entry),
           picture(panels[num],'Correlation scatter · #'+str(candidates[num]['rank'])) if num is not None else empty('Correlation scatter · 유의한 수치 인자 없음')]
    content+='<table role="presentation" width="100%" cellspacing="0" cellpadding="3" style="table-layout:fixed"><tr>'+''.join('<td width="33.33%" style="vertical-align:top">'+v+'</td>' for v in cells)+'</tr></table>'
    chosen=next((sp for sp in maps if sp is not None),None)
    if chosen is None:chosen=_ml_context_spatial(entry)
    content+='<div style="max-width:1080px;margin:0 auto">'+(picture(chosen,'Wafer map / Radius') if chosen is not None else empty('Wafer map / Radius · 공간 좌표 없음'))+'</div>'
    rest=[i for i in range(len(candidates)) if i not in (cat,num)]
    if rest:
        content+='<table role="presentation" width="100%" cellspacing="0" cellpadding="3" style="table-layout:fixed"><tr>'
        content+=''.join('<td style="vertical-align:top" width="'+str(100/len(rest))+'%">'+picture(panels[i],'#'+str(candidates[i]['rank'])+' '+candidates[i]['column'])+'</td>' for i in rest)+'</tr></table>'
    return content+'</section>'


def _ml_context_box(entry):
    """Descriptive root-level split box when no significant categorical factor exists."""
    import html
    p=entry['points']
    if p.empty:return '<p>Box · 자료 없음</p>'
    keys=[c for c in ('_vehicle','root_lot_id','_knob') if c in p]
    if 'root_lot_id' not in keys:return '<p>Box · root lot 정보 없음</p>'
    data=p.groupby(keys).agg(y=('_value','median'),recent=('_recent','max')).reset_index()
    data['x']=data['_vehicle'].astype(str)+' / '+data['_knob'].astype(str) if '_vehicle' in data else data['_knob'].astype(str)
    c=dict(kind='categorical',plot=data.to_dict('records'),rank=0,column='Split (descriptive)',effect=0,q=1)
    png=_ml_candidate_chart(entry,c)
    return '<div style="font-size:12px;color:#003366;padding:2px 6px">Box · Split</div><img alt="Split box" style="width:100%;height:auto;display:block" src="'+_img_datauri(png)+'">'


def _ml_context_spatial(entry):
    """New/history maps and radius with equal root weighting, never guessed geometry."""
    import io
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    raw=entry.get('spatial',pd.DataFrame())
    needed={'root_lot_id','wafer_id','chip_x_pos','chip_y_pos','_value','_recent'}
    if raw.empty or not needed.issubset(raw):return None
    if 'flat_zone' in raw and raw.flat_zone.nunique()>1:return None
    if '_vehicle' in raw:raw=raw.loc[raw['_vehicle'].eq(entry['vehicle'])]
    raw=raw.dropna(subset=list(needed)).copy()
    wafer=raw.groupby(['root_lot_id','wafer_id','_recent','chip_x_pos','chip_y_pos'])._value.median().reset_index()
    roots=wafer.groupby(['root_lot_id','_recent','chip_x_pos','chip_y_pos'])._value.median().reset_index()
    sites=roots.groupby(['_recent','chip_x_pos','chip_y_pos'])._value.median().reset_index()
    if sites.empty:return None
    fig,axes=plt.subplots(1,3,figsize=(12.4,2.6),layout='constrained')
    try:
        norm=Normalize(sites._value.min(),sites._value.max());sc=None
        for ax,recent in zip(axes[:2],[False,True]):
            s=sites.loc[sites['_recent'].eq(recent)]
            ax.set_title('New' if recent else 'History',fontsize=11)
            if s.empty:ax.text(.5,.5,'No data',ha='center',transform=ax.transAxes);continue
            sc=ax.scatter(s.chip_x_pos,s.chip_y_pos,c=s._value,cmap='viridis',norm=norm,marker='s',s=65)
            ax.set_aspect('equal');ax.set_xlabel('Shot X');ax.set_ylabel('Shot Y')
        if sc is not None:fig.colorbar(sc,ax=list(axes[:2]),shrink=.8,pad=.02)
        roots['radius']=np.hypot(roots.chip_x_pos,roots.chip_y_pos)
        for recent,g in roots.groupby('_recent'):
            profile=g.groupby(['root_lot_id','radius'])._value.median().groupby('radius').median()
            axes[2].plot(profile.index,profile.values,'o-',ms=3,label='New' if recent else 'History')
        axes[2].set_title('Radius · root median',fontsize=11);axes[2].set_xlabel('Shot-origin radius (grid)');axes[2].legend(fontsize=9)
        for ax in axes:ax.tick_params(labelsize=9)
        stream=io.BytesIO();fig.savefig(stream,format='png',dpi=130);return stream.getvalue()
    finally:plt.close(fig)


def _ml_influence_pack(entries,settings,title):
    import io,html
    from pptx import Presentation
    from pptx.util import Inches,Pt
    from pptx.dml.color import RGBColor
    esc=lambda x:html.escape(str(x))
    cache=[]
    for entry in entries:
        candidates=entry['_influence']['candidates']
        panels=[_ml_candidate_chart(entry,c) for c in candidates]
        spatial=[_ml_split_spatial(entry,c) for c in candidates]
        cache.append((entry,panels,spatial))
    def build(batch):
        prs=Presentation();prs.slide_width=Inches(13.333);prs.slide_height=Inches(7.5);sections=[]
        def text(slide,value,x,y,w,h,size=12):
            tf=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h)).text_frame
            # Long identifiers remain complete in slide notes and HTML.
            value=str(value)
            if h<=.6 and len(value)>int(w*9):
                slide.notes_slide.notes_text_frame.text+='\n'+value
                value=value[:max(1,int(w*9)-3)]+'...'
            tf.word_wrap=True;tf.margin_left=tf.margin_right=tf.margin_top=tf.margin_bottom=0;tf.text=value
            for p in tf.paragraphs:p.font.size=Pt(size)
        def pic(slide,png,x,y,w):slide.shapes.add_picture(io.BytesIO(png),Inches(x),Inches(y),width=Inches(w))
        def img(png):return '<img style="width:100%;height:auto;display:block" src="'+_img_datauri(png)+'">'
        for entry,panels,maps in batch:
            report=entry['_influence'];candidates=report['candidates']
            label=' / '.join(str(entry.get(k,'')) for k in ['vehicle','item','step','program','temperature'])
            slide=prs.slides.add_slide(prs.slide_layouts[6]);text(slide,label,.35,.15,12.6,.4,18)
            summary=_ml_finding_summary(entry)
            text(slide,summary[:230],.35,.6,12.6,.5,11)
            pic(slide,entry['png'],.35,1.12,6.1)
            headers=['# / factor','Evidence','effect / q / roots'];row_count=max(2,min(7,len(candidates)+1))
            table=slide.shapes.add_table(row_count,3,Inches(6.65),Inches(1.15),Inches(6.25),Inches(.4*row_count)).table
            table.columns[0].width=Inches(2.55);table.columns[1].width=Inches(1.8);table.columns[2].width=Inches(1.9)
            rows=[headers]+[[f"{c['rank']} {c['column']}",c.get('match_label','time-adjusted corr'),f"{c['effect']:.2f} / {c['q']:.2g} / {c['n']}"] for c in candidates]
            if not candidates:rows.append(['연관 후보 없음','탐지 변화는 유지','원인 미확정'])
            for i in range(row_count):
                for j in range(3):
                    cell=table.cell(i,j);cell.text=rows[i][j] if i<len(rows) else ''
                    cell.margin_top=cell.margin_bottom=Inches(.02)
                    cell.fill.solid();cell.fill.fore_color.rgb=RGBColor.from_string('E8EDF3' if i==0 else ('FAFBFC' if i%2 else 'FFFFFF'))
                    for p in cell.text_frame.paragraphs:p.font.size=Pt(10);p.font.color.rgb=RGBColor.from_string('172B43')
            if row_count<=4:
                text(slide,f"전체 {entry['lots']} lots / 신규 {entry.get('recent_lots',0)} lots / N={entry['n']}\n검정 실행 {entry.get('ml_test_count',0)}회 · 제외 사유는 HTML 자료 제한 확인",6.7,1.3+.4*row_count,6.1,.9,12)
            for j,png in enumerate(panels[:2]):pic(slide,png,.35+6.4*j,4.05,6.15)
            if not panels:text(slide,'유의한 연관 후보 없음 — 이상 탐지 결과는 유지됩니다. 제외 사유는 HTML/분석 JSON에서 확인하세요.',.5,4.4,12,1,16)
            text(slide,'탐색적 연관성 · 인과 아님 | effect: 범주=순위 효과크기, 수치=시간 보정 상관 | 점/표본 수=root lot',.4,6.9,12.5,.3,10)
            # Every candidate gets a readable detail page, including ranks 5+.
            for c,png,sp in zip(candidates,panels,maps):
                detail=prs.slides.add_slide(prs.slide_layouts[6])
                text(detail,label+' · 연관 후보 #'+str(c['rank']),.35,.15,12.6,.4,18)
                text(detail,c['column']+' / '+str(c['comparison']),.4,.65,12.4,.6,14)
                pic(detail,png,.4,1.35,6.1)
                evidence=(f"{c.get('match_label','시간 보정 상관')}\nroot lots={c['n']} / coverage={c['coverage']:.0%}\n"
                          f"effect={c['effect']:.3g} / q={c['q']:.3g}\nclean roots={c.get('clean_roots',0)} / similar roots={c.get('similar_roots',0)}\n"
                          '비교 조건과 lot 구성 확인 후 공정 이력을 대조하세요.')
                text(detail,evidence,6.8,1.4,5.9,2.4,14)
                if sp is not None:pic(detail,sp,.4,4.0,12.4)
                else:text(detail,'공간 상세 없음: 공간 좌표 또는 비교 가능한 범주형 조건이 없습니다.',.4,4.3,12.3,.8,14)
                detail.notes_slide.notes_text_frame.text+='\nMatched controls: '+', '.join(c.get('control_columns',[]))
                text(detail,'탐색적 연관성 · 원인 확정 아님. 공간값은 raw 값, 검정은 root lot 요약값 기준.',.4,7.08,12.4,.25,10)
            sections.append(_ml_compact_html(entry,panels,maps,settings))
        body=_service_html_start(title,'', '')
        body+=''.join(sections)+'</div></body></html>'
        sources=re.findall(r'<img\s[^>]*?src="([^"]*)"',body,re.DOTALL)
        if any(not s.startswith('data:image/') for s in sources):raise ValueError('HTML 인라인 이미지 불변식 위반')
        print('[INFO] HTML 인라인 이미지 검증 OK',flush=True)
        _service_deck_style(prs)
        stream=io.BytesIO();prs.save(stream);return body,stream.getvalue()
    result=[];batch=[]
    for cached in cache:
        proposed=batch+[cached];body,ppt=build(proposed)
        if len(body.encode())>=min(2000000,int(settings.get('html_max_bytes',2000000))) or len(ppt)>=min(10000000,int(settings.get('ppt_max_bytes',10000000))):
            if not batch:raise _DailyTrendLimit('ML 항목 상세가 용량 제한을 초과했습니다. influence_top_k를 줄이세요.')
            b,p=build(batch);result.append((b,p,len(batch)));batch=[cached]
        else:batch=proposed
    if batch:
        b,p=build(batch)
        if len(b.encode())>=min(2000000,int(settings.get('html_max_bytes',2000000))) or len(p)>=min(10000000,int(settings.get('ppt_max_bytes',10000000))):raise _DailyTrendLimit('ML 단일 항목 용량 초과')
        result.append((b,p,len(batch)))
    if len(result)>min(10,int(settings.get('max_mail_parts',10))):raise _DailyTrendLimit('ML 메일 최대 분할 수 초과')
    return result


def _daily_trend_pack(entries, settings, title):
    """Try one PPT/mail first; split by measured PPT/HTML bytes, never by dropping items."""
    if settings.get('service')=='mlmode' and entries:
        owners=[e for e in entries if not e.get('parent_item')]
        for e in owners:e.setdefault('_influence',dict(candidates=[],skipped=[]))
        return _ml_influence_pack(owners,settings,title)
    import io,html
    from pptx import Presentation
    from pptx.util import Inches,Pt
    category_colors=['#d0e2ff','#d9fbfb','#e8daff','#defbe6','#fff1e6','#ffd7d9']
    category_fill={key:category_colors[i%len(category_colors)] for i,key in enumerate(sorted({(e['vehicle'],e['category']) for e in entries}))}
    ml_mode=settings.get('service')=='mlmode'
    def draw_legend(slide,rows,x,y,columns=1):
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        for j,row in enumerate(rows):
            col=j%columns;line=j//columns
            line_height=.22 if ml_mode else .145
            col_width=3.1 if ml_mode else 1.65
            swatch=slide.shapes.add_shape(MSO_SHAPE.OVAL,Inches(x+col*col_width),Inches(y+line*line_height+.06),Inches(.08),Inches(.08))
            swatch.fill.solid();swatch.fill.fore_color.rgb=RGBColor.from_string(row['color'][1:])
            swatch.line.color.rgb=RGBColor.from_string('000000' if row['highlight'] else row['color'][1:])
            box=slide.shapes.add_textbox(Inches(x+.12+col*col_width),Inches(y+line*line_height),Inches(2.95 if ml_mode else 4.7),Inches(line_height)).text_frame
            box.margin_left=box.margin_right=box.margin_top=box.margin_bottom=0
            box.text=row['label'];box.paragraphs[0].font.size=Pt(10 if ml_mode else 8)
    def build(batch):
        prs=Presentation();prs.slide_width=Inches(13.333);prs.slide_height=Inches(7.5)
        rows=[];cards=[];categories={};overflow=[]
        for i,entry in enumerate(batch):
            if ml_mode or i%2==0:
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                tf=slide.shapes.add_textbox(Inches(.3),Inches(.08),Inches(12.7),Inches(.42)).text_frame
                tf.text=title;tf.paragraphs[0].font.size=Pt(18)
            y=.6 if ml_mode else .6+(i%2)*3.35
            label=f"{entry['vehicle']} / {entry['category']} / {entry['item']} / {entry['step']} / {entry['program']} / {entry['temperature']}"
            box=slide.shapes.add_textbox(Inches(.35),Inches(y),Inches(12.6),Inches(.35)).text_frame
            box.margin_top=box.margin_bottom=0
            box.word_wrap=True;box.text=label
            for p in box.paragraphs:p.font.size=Pt(10 if len(label)>125 else 13)
            spatial=' / spatial / ' in entry['item']
            if not spatial and not entry['points'].empty and '_ppt_png' not in entry:
                entry['_ppt_png']=_daily_trend_chart(entry,dict(settings,_ppt_chart=True,chart_dpi=max(120,int(settings.get('chart_dpi',150)))))
            chart_width=(12.4 if spatial else 8.8) if ml_mode else (12.4 if spatial else 7.2)
            chart_height=(3.6 if spatial else 8.8*3.2/7.2) if ml_mode else 2.65
            slide.shapes.add_picture(io.BytesIO(entry.get('_ppt_png',entry['png'])),Inches(.4),Inches(y+.36),width=Inches(chart_width),height=Inches(chart_height))
            if ml_mode or not spatial:
                legend_rows=entry.get('_legend_rows',[])
                cap=16 if ml_mode and spatial else 18
                draw_legend(slide,legend_rows[:cap],.5 if ml_mode and spatial else (9.5 if ml_mode else 7.8),
                            4.75 if ml_mode and spatial else y+.36,columns=4 if ml_mode and spatial else 1)
                if len(legend_rows)>cap:overflow.append((entry,legend_rows[cap:]))
                if not ml_mode and len(legend_rows)<=8:
                    state,_,action=_trend_review(entry)
                    info=slide.shapes.add_textbox(Inches(7.8),Inches(y+.65+.145*len(legend_rows)),Inches(4.9),Inches(1.35)).text_frame
                    info.word_wrap=True;info.margin_top=info.margin_bottom=0
                    info.text=state+'\n'+f"전체 {entry['lots']} lots / 신규 {entry.get('recent_lots',0)} lots\n"+action
                    for p in info.paragraphs:p.font.size=Pt(12)
            pct='N/A' if entry['out_pct'] is None else f"{entry['out_pct']:.1f}%"
            metric='' if settings.get('service')=='mlmode' else f' / spec out={pct}'
            note=f"N={entry['n']} / lots={entry['lots']} / recent lots={entry.get('recent_lots',0)}{metric} / {entry['aggregation']} / {entry['reason']}"
            if ml_mode:
                explanation=slide.shapes.add_textbox(Inches(.4),Inches(5.7),Inches(12.4),Inches(.65)).text_frame
                explanation.word_wrap=True;explanation.margin_top=explanation.margin_bottom=0
                explanation.text=_ml_reading_note(entry)
                for p in explanation.paragraphs:p.font.size=Pt(12)
                note_box=slide.shapes.add_textbox(Inches(.4),Inches(6.4),Inches(12.4),Inches(.85)).text_frame
                note_box.word_wrap=True;note_box.margin_top=note_box.margin_bottom=0
                note_box.text=f"N={entry['n']:,} / lots={entry['lots']} / 신규 lots={entry.get('recent_lots',0)}\n"+_ml_finding_summary(entry)
                for p in note_box.paragraphs:p.font.size=Pt(11)
            else:
                note_box=slide.shapes.add_textbox(Inches(.4),Inches(y+3.04),Inches(12.4),Inches(.25)).text_frame
                note_box.text=(_trend_review(entry)[0]+' / '+note)[:210];note_box.paragraphs[0].font.size=Pt(8)
            knobs=sorted(entry['points']['_knob'].unique()) if not entry['points'].empty else []
            slide.notes_slide.notes_text_frame.text+='\n'+'\n'.join([label,note,*entry['warnings'],entry['knob_label'],'Knobs: '+'; '.join(knobs)])
            cells=[entry['vehicle'],entry['category'],entry['item'],entry['step'],entry['program'],entry['temperature'],
                   entry['n'],entry['lots'],entry.get('recent_lots',0),entry['reason'],' / '.join(entry['warnings']),i+1 if ml_mode else i//2+1]
            if settings.get('service')!='mlmode':cells.insert(9,pct)
            rows.append('<tr>'+''.join('<td>'+html.escape(str(c))+'</td>' for c in cells)+'</tr>')
            uri=entry.get('_inline_uri')
            if uri is None:
                if settings.get('service')=='daily_trend' and len(entry['png'])>int(getattr(GLOBAL_CONFIG,'html_inline_img_max_kb',100) or 100)*1024:
                    raise _DailyTrendLimit('차트 한 장이 인라인 이미지 한도를 초과했습니다. 항목/범위를 조정해 주세요. 화질은 자동으로 낮추지 않습니다.')
                uri=_img_datauri(entry['png']);entry['_inline_uri']=uri
            card_layout='max-width:1080px;margin:0 auto;' if settings.get('service')=='mlmode' else 'margin:0;'
            reading=''
            if ml_mode:
                legend_html=' '.join('<span style="display:inline-block;margin:2px 12px 2px 0;color:#161616">'
                                     '<span style="color:'+r['color']+'">●</span> '+html.escape(r['label'])+'</span>'
                                     for r in entry.get('_legend_rows',[]))
                reading='<div style="padding:8px 12px;font-size:13px;line-height:1.6">'+legend_html+'<p style="margin:6px 0">'+html.escape(_ml_reading_note(entry))+'</p>'
                reading+=f"<p style='margin:4px 0'>N={entry['n']:,} / lots={entry['lots']} / 신규 lots={entry.get('recent_lots',0)}</p>"
                if entry['warnings']:reading+='<p style="margin:4px 0;color:#8a3800">확인 사항: '+html.escape(' / '.join(dict.fromkeys(entry['warnings'])))+'</p>'
                reading+='</div>'
            else:
                reading=''
            cards.append('<section style="'+card_layout+'border:1px solid #cbd5df;border-radius:0;overflow:hidden;background:white">'
                         '<h3 style="margin:0;padding:4px 6px;background:#e8edf3;color:#003366;font-size:14px;font-weight:600;line-height:18px">'+html.escape(entry['category']+' · '+entry['item']+' / '+entry['step']+' / '+entry['program']+' / '+str(entry['temperature']))+'</h3>'
                         '<img alt="'+html.escape(label,quote=True)+'" width="700" style="display:block;width:100%;max-width:100%;height:auto" src="'+uri+'">'
                         +reading+'</section>')
            categories.setdefault((entry['vehicle'],entry['category']),[]).append((cards[-1],spatial,entry))
        for entry,rest in overflow:
            for start in range(0,len(rest),18):
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                tf=slide.shapes.add_textbox(Inches(.4),Inches(.1),Inches(12),Inches(.5)).text_frame
                tf.text=entry['item']+' / legend continued';tf.paragraphs[0].font.size=Pt(18)
                spatial=' / spatial / ' in entry['item']
                slide.shapes.add_picture(io.BytesIO(entry.get('_ppt_png',entry['png'])),Inches(.4),Inches(.9),width=Inches(7.2),height=Inches(7.2*3.6/12.4 if spatial and ml_mode else (3.2 if ml_mode else 2.65)))
                draw_legend(slide,rest[start:start+18],9.5 if ml_mode else 7.8,.9)
        body=_service_html_start(title,'이상 후보의 탐지 근거와 후속 검토' if ml_mode else '',str(settings.get('report_now','')))
        body+='<nav style="padding:8px 0;border-bottom:1px solid #e0e0e0">'+ ' &nbsp; '.join('<a style="color:#0f62fe;font-size:14px;display:inline-block;padding:4px 8px" href="#cat'+str(i)+'">'+html.escape('/'.join(key))+' ('+str(len(group))+')</a>' for i,(key,group) in enumerate(categories.items()))+'</nav>'
        for i,(key,group) in enumerate(categories.items()):
            body+='<div class="trend-category"><h2 id="cat'+str(i)+'" style="position:sticky;top:0;z-index:5;font-size:16px;font-weight:600;background:'+category_fill[key]+';border-left:3px solid #0f62fe;padding:8px;margin:16px 0 4px">'+html.escape(' / '.join(key))+' · '+str(len(group))+' <a href="#top" style="float:right;color:#0f62fe;font-size:12px;font-weight:400">목록 ↑</a></h2><table role="presentation" width="100%" style="table-layout:fixed;border-spacing:4px">'
            columns=max(1,min(6,int(settings.get('html_columns',4))))
            used=0;body+='<tr>'
            for card,spatial,card_entry in group:
                # ML gives each item a large trend row, followed by its spatial panels.
                span=columns if settings.get('service')=='mlmode' else (min(3,columns) if spatial else 1)
                if used+span>columns:
                    if used<columns:body+='<td colspan="'+str(columns-used)+'"></td>'
                    body+='</tr><tr>';used=0
                body+='<td colspan="'+str(span)+'" width="'+str(100*span/columns)+'%" style="vertical-align:top">'+card+'</td>'
                used+=span
                if used==columns:body+='</tr><tr>';used=0
                if settings.get('service')=='mlmode':
                    current=card_entry.get('parent_item',card_entry['item'])
                    index=next(j for j,v in enumerate(group) if v[2] is card_entry)
                    next_item=group[index+1][2].get('parent_item',group[index+1][2]['item']) if index+1<len(group) else None
                    if next_item!=current:
                        if used:body+='<td colspan="'+str(columns-used)+'"></td></tr><tr>';used=0
                        owner=next((v[2] for v in group if v[2]['item']==current),card_entry)
                        findings=owner.get('ml_findings',[])
                        names={'isolation_forest':'Isolation Forest 이상 증가','local_outlier_factor':'주변 패턴 대비 이상 증가','spatial_pattern':'웨이퍼 공간 패턴 변화','time_trend':'시간 추이 변화','split_difference':'Split 간 차이','equipment_difference':'장비 간 차이','spike_rate':'극단값 비율 증가'}
                        summary=' · '.join(dict.fromkeys(names.get(f['module'],f['module']) for f in findings)) or owner['reason']
                        body+='<td colspan="'+str(columns)+'" style="padding:8px 12px;background:#f4f4f4;font-size:13px"><b>'+html.escape(current)+' 탐지 근거</b> — '+html.escape(summary)
                        if findings:body+=' (보정 q 최소 '+format(min(f['q'] for f in findings),'.3g')+')'
                        body+='</td></tr><tr>'
            if used:body+='<td colspan="'+str(columns-used)+'"></td>'
            body+='</tr>'
            body+='</table></div>'
        body+='</div></body></html>'
        sources=re.findall(r'<img\s[^>]*?src="([^"]*)"',body,re.DOTALL)
        if len(sources)!=len(batch) or any(not s.startswith('data:image/') for s in sources):raise ValueError('HTML 인라인 이미지 불변식 위반')
        _service_deck_style(prs)
        stream=io.BytesIO();prs.save(stream);ppt=stream.getvalue()
        return body,ppt
    def fits(body,ppt):
        # Independent artifact limits: inline base64 is already included in HTML bytes.
        return (len(ppt)<min(10_000_000,int(settings.get('ppt_max_bytes',10_000_000))) and
                len(body.encode('utf-8'))<min(2_000_000,int(settings.get('html_max_bytes',2_000_000))))
    if not entries:raise ValueError('Daily Trend: 선택 제품에 CAT2 항목이 없습니다')
    parts=[];remaining=sorted(entries,key=lambda e:_trend_category_key(e,settings))
    while remaining:
        if settings.get('service')=='daily_trend' and len(parts)>=min(10,max(1,int(settings.get('max_mail_parts',10)))):
            raise _DailyTrendLimit('리포트가 최대 10통을 초과합니다. 선택 제품·카테고리·항목 범위를 줄여 주세요. 보고서 메일은 발송하지 않았습니다.')
        body,ppt=build(remaining)
        if fits(body,ppt):parts.append((body,ppt,len(remaining)));break
        # Largest fitting prefix avoids producing four half-sized mails when three suffice.
        low,high=1,len(remaining)-1;best=None
        while low<=high:
            mid=(low+high)//2;body,ppt=build(remaining[:mid])
            if fits(body,ppt):best=(body,ppt,mid);low=mid+1
            else:high=mid-1
        if best is None:
            if settings.get('service')=='daily_trend':raise _DailyTrendLimit('차트 한 장이 메일 용량 한도를 초과합니다. 선택 항목/범위를 조정해 주세요. 화질은 자동으로 낮추지 않습니다.')
            raise ValueError('Daily Trend 단일 차트가 용량 제한을 초과합니다.')
        # Keep whole categories together where possible; only oversized categories split.
        boundaries=[j for j in range(1,best[2]+1) if j==len(remaining) or (remaining[j-1]['vehicle'],remaining[j-1]['category'])!=(remaining[j]['vehicle'],remaining[j]['category'])]
        if boundaries and boundaries[-1]!=best[2]:
            n=boundaries[-1];body,ppt=build(remaining[:n]);best=(body,ppt,n)
        parts.append(best);remaining=remaining[best[2]:]
    print(f'[INFO] HTML 인라인 이미지 검증 OK / {len(entries)} charts / {len(parts)} parts')
    return parts


def _daily_trend_report(request):
    import hashlib,json
    settings=dict(request['settings']);products=settings.get('products') or []
    if request.get('send') and not settings.get('recipients'):
        return dict(status='disabled',reason='지정 수신처 없음; 발행 생략')
    service=request.get('service','daily_trend')
    if service not in ('daily_trend','mlmode'):raise ValueError('Unknown report service')
    if not products:raise ValueError('daily_trend.products에 선택 제품을 지정하세요')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+',str(p)) for p in products):raise ValueError('잘못된 제품 키')
    identity=request['id']+'-'+hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()[:12]
    dest=os.path.join(operations_root(),service,re.sub(r'[^A-Za-z0-9_.-]','_',identity))
    settings['service']=service
    settings['report_now']=pd.Timestamp.fromtimestamp(request['now'])
    settings['highlight_since']=pd.Timestamp.fromtimestamp(request['now']).normalize()
    # Separate checkpoint per service/source selection, unaffected by daily date or recipients.
    checkpoint_key=service+'|'+hashlib.sha256(json.dumps([products,settings.get('with_vehicle',{})],sort_keys=True).encode()).hexdigest()
    checkpoint=ops_get('trend_publication_checkpoints',checkpoint_key,{})
    settings['_published_observations']=set(checkpoint['observations']) if 'observations' in checkpoint else None
    settings['_candidate_observations']=set()
    os.makedirs(dest,exist_ok=True)
    manifest_path=os.path.join(dest,'manifest.json')
    manifest=None
    if os.path.exists(manifest_path):
        with open(manifest_path,encoding='utf-8') as stream:manifest=json.load(stream)
        # Never rebuild an already attempted report into a different set of parts.
        for part in manifest['parts']:
            for key in ('html','ppt'):
                with open(part[key],'rb') as stream:digest=hashlib.sha256(stream.read()).hexdigest()
                if digest!=part[key+'_sha256']:raise ValueError('Daily Trend 저장 산출물 변경 감지; 발송 이력 확인 필요')
    if manifest is None:
        entries=[];coverage=[];companions={};sources=list(products);source_entries={}
        if service=='mlmode':
            for vehicle in products:
                GLOBAL_CONFIG.load_from_yaml(vehicle)
                companion=(settings.get('with_vehicle') or {}).get(vehicle,GLOBAL_CONFIG.get('with_vehicle',[])) or []
                if isinstance(companion,str):companion=[companion]
                companions[vehicle]=[p for p in companion if p!=vehicle]
                sources.extend(companions[vehicle])
        for vehicle in dict.fromkeys(sources):
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',str(vehicle)):raise ValueError('잘못된 with_vehicle 제품 키')
            GLOBAL_CONFIG.load_from_yaml(vehicle)
            formatter=pd.read_csv(os.path.join('reformatter',vehicle+'_reformatter.csv'))
            coordinate_path=GLOBAL_CONFIG.get('coordinate_file_path')
            if coordinate_path and os.path.exists(coordinate_path):
                set_chip_layout(pd.read_excel(coordinate_path,sheet_name='Zone_Define'))
            if 'CAT2' not in formatter:formatter['CAT2']=''
            if service=='mlmode':formatter['CAT2']=formatter['CAT2'].fillna('').replace(r'^\s*$','Uncategorized',regex=True)
            selected=formatter.loc[formatter['CAT2'].notna() & formatter['CAT2'].astype(str).str.strip().ne('')]
            if selected.empty:
                coverage.append(dict(vehicle=vehicle,status='no_category',items=0));continue
            frame=daily_trend_load(vehicle,formatter)
            product_entries=daily_trend_entries(frame,formatter,vehicle,settings,pd.Timestamp.fromtimestamp(request['now']))
            source_entries[vehicle]=product_entries
            coverage.append(dict(vehicle=vehicle,status='ok' if not frame.empty else 'no_data',items=len(product_entries),
                                 viewing_period=GLOBAL_CONFIG.get('viewing_period',30)))
        for vehicle in products:
            for entry in source_entries.get(vehicle,[]):
                if service=='mlmode':
                    signature=lambda e:tuple(e[k] for k in ('item','step','program','temperature','unit','aggregation','x_label','knob_label'))
                    points=[entry['points'].assign(_vehicle=vehicle)]
                    spatial=[entry.get('spatial',pd.DataFrame()).assign(_vehicle=vehicle)]
                    for companion in companions.get(vehicle,[]):
                        points.extend(e['points'].assign(_vehicle=companion) for e in source_entries.get(companion,[]) if signature(e)==signature(entry))
                        spatial.extend(e.get('spatial',pd.DataFrame()).assign(_vehicle=companion) for e in source_entries.get(companion,[]) if signature(e)==signature(entry))
                    entry['points']=pd.concat(points,ignore_index=True)
                    entry['spatial']=pd.concat(spatial,ignore_index=True)
                    entry['comparison_products']=companions.get(vehicle,[])
                    entry['n']=len(entry['points'])
                    if not entry['points'].empty:
                        entry['lots']=len(entry['points'][['_vehicle','fab_lot_id']].drop_duplicates())
                        entry['recent_lots']=len(entry['points'].loc[entry['points']['_recent'],['_vehicle','fab_lot_id']].drop_duplicates())
                entries.append(entry)
        analysis={}
        catalog_entries=entries
        if service=='mlmode':
            entries,analysis=ml_trend_select(entries,settings)
            if settings.get('influence_enabled',True):analysis['influence']=ml_influence_analyze(entries,settings)
            else:
                for entry in entries:entry['_influence']=dict(candidates=[],skipped=[])
        settings['_coverage']=coverage;settings['_analysis']=analysis
        entries.sort(key=lambda e:(e['vehicle'],e['category'],e['item'],e['step'],e['program'],e['temperature']))
        plot_entries=[]
        for i,entry in enumerate(entries,1):
            entry['png']=_daily_trend_chart(entry,settings)
            plot_entries.append(entry)
            if service=='mlmode' and '_influence' not in entry:plot_entries.extend(_ml_spatial_details(entry,settings))
            if i%25==0:print(f'[INFO] Daily Trend charts {i}/{len(entries)}',flush=True)
        title=('Auto Report · ML Insight' if service=='mlmode' else 'Auto Report · Daily Trend')+' / '+', '.join(products)+' / '+datetime.fromtimestamp(request['now']).strftime('%Y-%m-%d')
        summary_artifact=None
        if service=='mlmode' and not entries:
            status_text='분석 자료 부족 · 실행 가능한 검정 없음' if not analysis.get('tested_items') else '실행된 검정에서 설정 기준을 만족하는 후보 없음'
            content=_service_html_start(title,'분석 범위와 자료 상태 확인',str(settings['report_now']))
            content+='<p style="padding:10px;background:#fff8ee;color:#8a3800">'+status_text+'</p>'
            content+=_service_heading('항목별 분석 상태')+_service_table(['제품 / 항목','Step / 프로그램 / 온도','상태','검정 수','자료 제한'],
                 [[e['vehicle']+' / '+e['item'],e['step']+' / '+e['program']+' / '+str(e['temperature']),_trend_review(e,True)[0],e.get('ml_test_count',0),' / '.join(dict.fromkeys(e.get('warnings',[])))] for e in catalog_entries])+'</div></body></html>'
            hp=os.path.join(dest,'analysis-summary.html');atomic_bytes(hp,content.encode('utf-8'))
            summary_artifact=dict(html=hp,html_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest())
        notice=None
        try:chunks=_daily_trend_pack(plot_entries,settings,title) if entries or service!='mlmode' else []
        except _DailyTrendLimit as exc:
            import html
            chunks=[]
            content='<html><meta charset="utf-8"><body><h2>Daily Trend 발행 범위를 조정해 주세요</h2><p>'+html.escape(str(exc))+'</p><p>제품: '+html.escape(', '.join(products))+' / 차트: '+str(len(plot_entries))+'</p><p>HTML 본문은 2MB 이하, PPTX 첨부는 별도로 10MB 이하, 한 번의 발행은 최대 10통입니다. My_config.py의 daily_trend 설정과 제품 reformatter의 카테고리/항목 선택을 조정해 주세요.</p></body></html>'
            hp=os.path.join(dest,'limit-notice.html');atomic_bytes(hp,content.encode('utf-8'))
            notice=dict(html=hp,html_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),title=title+' / 발행 범위 조정 요청',reason=str(exc))
        parts=[]
        for i,(body,ppt,count) in enumerate(chunks,1):
            stem=f'{service}-{i:02d}-of-{len(chunks):02d}'
            hp=os.path.join(dest,stem+'.html');pp=os.path.join(dest,stem+'.pptx')
            atomic_bytes(hp,body.encode('utf-8'));atomic_bytes(pp,ppt)
            parts.append(dict(html=hp,ppt=pp,count=count,html_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(),
                              ppt_sha256=hashlib.sha256(ppt).hexdigest(),title=title+(f' ({i}/{len(chunks)})' if len(chunks)>1 else '')))
        # A local, lossless index also records full knob values and diagnostics.
        catalog=[]
        for entry in catalog_entries:
            row={k:v for k,v in entry.items() if k not in ('points','png','spatial','_inline_uri','_ppt_png','_legend_rows','_influence')}
            row['knob_values']='; '.join(sorted(entry['points']['_knob'].unique())) if not entry['points'].empty else ''
            catalog.append(row)
        atomic_bytes(os.path.join(dest,'catalog.csv'),pd.DataFrame(catalog).to_csv(index=False).encode('utf-8-sig'))
        if service=='mlmode':
            audit=[dict(vehicle=e['vehicle'],item=e['item'],step=e['step'],program=e['program'],temperature=e['temperature'],analysis=e.get('_influence',{})) for e in entries]
            atomic_bytes(os.path.join(dest,'influence.json'),json.dumps(audit,ensure_ascii=False,indent=2,default=str).encode('utf-8'))
        manifest=dict(id=identity,parts=parts,notice=notice,summary_artifact=summary_artifact,coverage=coverage,items=len(entries),detail_panels=len(plot_entries)-len(entries),created=request['now'],analysis=analysis,
                      checkpoint_key=checkpoint_key,observations=sorted(settings['_candidate_observations']))
        atomic_bytes(manifest_path,json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    if manifest.get('notice'):
        with open(manifest['notice']['html'],'rb') as stream:notice_digest=hashlib.sha256(stream.read()).hexdigest()
        if notice_digest!=manifest['notice']['html_sha256']:raise ValueError('안내 메일 저장 산출물 변경 감지')
    if manifest.get('summary_artifact'):
        summary_artifact=manifest['summary_artifact']
        with open(summary_artifact['html'],'rb') as stream:summary_digest=hashlib.sha256(stream.read()).hexdigest()
        if summary_digest!=summary_artifact['html_sha256']:raise ValueError('ML 분석 요약 저장 산출물 변경 감지')
    GLOBAL_CONFIG.load_from_yaml(settings.get('mail_vehicle') or products[0])
    result=dict(manifest,status='preview',preview_only=not request.get('send',False),manifest=manifest_path)
    if service=='mlmode' and not manifest['parts'] and not manifest.get('notice'):
        result['status']='no_findings' if manifest['analysis'].get('tested_items',manifest['analysis'].get('statistical_tests',0)) else 'insufficient_data'
        ops_put('mlmode_reports',identity,result)
        if request.get('send') and result['status']=='no_findings':
            ops_put('trend_publication_checkpoints',checkpoint_key,dict(observations=manifest['observations'],completed=request['now'],id=identity))
        print('[INFO] ML mode: '+result['status']+'; 분석 범위/자료 제한은 analysis-summary.html 및 catalog.csv 확인')
        return result
    if request.get('send'):
        addresses={}
        for receiver in settings.get('recipients') or []:
            for recipient in _trigger_receivers(GLOBAL_CONFIG.get('email_list_path'),receiver):
                addresses[recipient['email'].lower()]=recipient
        if not addresses:raise ValueError('Daily Trend 수신처 미설정')
        receivers=[dict(r,seq=i) for i,r in enumerate(addresses.values(),1)]
        if manifest.get('notice'):
            notice=manifest['notice']
            states=[_durable_mail(service+'|'+identity+'|limit-notice',receivers,notice['title'],notice['html'],None,GLOBAL_CONFIG)]
            result['notification_only']=True
        else:
            # Guard replayed manifests too; no report is sent before the entire plan passes.
            if service=='daily_trend' and len(manifest['parts'])>10:raise ValueError('저장된 보고서가 10통을 초과합니다. 새 발행 ID로 다시 계획하세요.')
            states=[_durable_mail(service+'|'+identity+f'|part-{i}',receivers,part['title'],part['html'],part['ppt'],GLOBAL_CONFIG)
                    for i,part in enumerate(manifest['parts'],1)]
        result['status']='sent' if all(s=='sent' for s in states) else ('unknown' if 'unknown' in states else 'failed')
        if result['status']=='sent' and not manifest.get('notice'):
            ops_put('trend_publication_checkpoints',checkpoint_key,dict(observations=manifest['observations'],completed=request['now'],id=identity))
    ops_put(service+'_reports',identity,result)
    print(f"[INFO] {service} {result['status']}: {manifest['items']} charts / {len(manifest['parts'])} mail parts / {dest}")
    return result


def _watchdog_findings(observations, settings, now=None):
    from collections import defaultdict
    now=pd.Timestamp.now() if now is None else pd.Timestamp(now)
    start=now-pd.Timedelta(days=int(settings.get('analysis_days',14)))
    groups=defaultdict(list)
    for row in observations:
        if start <= pd.Timestamp(row['time']) <= now:
            groups[tuple(row['context'])].append(row)
    findings=[]
    for context, rows in groups.items():
        rows=sorted(rows,key=lambda r:r['time'])
        # A repeated run of one lot cannot satisfy recurrence / low-score criteria.
        lots={}
        for row in rows:lots[row['prime_key']]=row
        latest=sorted(lots.values(),key=lambda r:r['time'])
        minimum=max(3,int(settings.get('min_lots',3)))
        if len(latest)<minimum:continue
        if pd.Timestamp(latest[-1]['time']) < now-pd.Timedelta(days=float(settings.get('active_days',3))):continue
        recent=latest[-minimum:]
        low_limit=float(settings.get('low_score_pct',95))
        out_limit=float(settings.get('spec_out_pct',1))
        low=all(r['score'] is not None and r['score']<low_limit for r in recent)
        recurring=all(r['out'] is not None and r['out']/r['n']*100>=out_limit for r in recent)
        med=np.array([r['median'] for r in latest],dtype=float)
        drift=False;delta=0.;normalized=0.
        if len(latest)>=max(6,minimum*2):
            n=max(3,len(latest)//2)
            before=med[:n]; after=med[-n:]
            delta=float(np.median(after)-np.median(before))
            noise=max(float(np.std(before)), float(np.median([r['std'] for r in latest[:n]])),1e-12)
            normalized=abs(delta)/noise
            required=float(settings.get('shift_sigma',2))
            # Both persistent level change and sustained chronological drift are informative.
            times=np.array([(pd.Timestamp(r['time'])-pd.Timestamp(latest[0]['time'])).total_seconds()/86400 for r in latest])
            correlation=float(np.corrcoef(times,med)[0,1]) if np.std(times)>0 and np.std(med)>0 else 0.
            span=(latest[-1]['high']-latest[-1]['low']) if latest[-1]['high'] is not None and latest[-1]['low'] is not None else 0.
            material=abs(delta)>=abs(span)*float(settings.get('shift_min_spec_frac',.02))
            drift=material and normalized>=required and (abs(correlation)>=float(settings.get('trend_correlation',.7)) or
                                           all((after>np.median(before)) if delta>0 else (after<np.median(before))))
        reasons=[]
        if recurring:reasons.append(f'최근 {minimum} lot 연속 Spec out ≥ {out_limit:g}%')
        if low:reasons.append(f'최근 {minimum} lot 연속 score < {low_limit:g}%')
        if drift:reasons.append(f'분포 중심 변화 Δ={delta:.4g} ({normalized:.2f}σ)')
        if not reasons:continue
        count=sum(r['n'] for r in latest); outside=sum(r['out'] or 0 for r in latest)
        findings.append(dict(context=context,rows=latest,latest=latest[-1],reason=' / '.join(reasons),
                             lots=len(latest),n=count,out_pct=outside/count*100 if latest[-1]['out'] is not None else None,
                             score=latest[-1]['score'],delta=delta,shift_sigma=normalized))
    return sorted(findings,key=lambda f:(f['latest']['vehicle'],f['latest']['category'],f['latest']['item']))


def _watchdog_deck(findings, summary, settings):
    import io
    from pptx import Presentation
    from pptx.util import Inches,Pt
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['axes.unicode_minus']=False
    for dpi in (140,110,85,65,45):
        prs=Presentation();prs.slide_width=Inches(13.333);prs.slide_height=Inches(7.5)
        def title(slide,text,top=.2,size=20):
            box=slide.shapes.add_textbox(Inches(.35),Inches(top),Inches(12.6),Inches(.65)).text_frame
            box.word_wrap=True;box.text=text if len(text)<=200 else text[:197]+'...'
            for paragraph in box.paragraphs:paragraph.font.size=Pt(min(size,12) if len(text)>130 else size)
        slide=prs.slides.add_slide(prs.slide_layouts[6]);title(slide,'Auto Report · Daily Watchdog')
        title(slide,summary,1.2,15)
        title(slide,f"관측 {settings.get('analysis_days',14)}일 / 최소 {settings.get('min_lots',3)}개 lot / "
                    f"저점수 < {settings.get('low_score_pct',95)}% / 반복 Spec out ≥ {settings.get('spec_out_pct',1)}%",2.5,13)
        title(slide,'비교 조건: 제품 · Step · 측정 프로그램 · 온도 · FULL/13pt · Spec/집계 규칙을 동일하게 분리',3.4,13)
        title(slide,'각 페이지: lot 중앙값 Trend + 전체 값 분포(24-bin 요약)와 Spec out 비율\n'
                    '집계 항목은 설정된 wafer/측정 집계값으로 계산하며 raw shot으로 대체하지 않습니다.',4.2,13)
        if not findings:title(slide,'관측 자료에서 설정 조건을 만족하는 항목 없음 (자료 부족은 정상 판정이 아님)',5.5,15)
        for index,finding in enumerate(findings,2):
            latest=finding['latest'];rows=finding['rows']
            slide=prs.slides.add_slide(prs.slide_layouts[6])
            title(slide,f"{latest['vehicle']} / {latest['step']} / {latest['category']} / {latest['item']}",size=17)
            title(slide,finding['reason'],.9,12)
            fig,axes=plt.subplots(1,2,figsize=(12.2,4.2))
            fig.subplots_adjust(left=.07,right=.98,bottom=.22,top=.87,wspace=.3)
            points=sorted(rows,key=lambda r:r['time'])
            axes[0].plot([pd.Timestamp(r['time']) for r in points],[r['median'] for r in points],'-o',ms=3,color='#2266aa')
            axes[0].set_title('Lot / measurement median trend',fontsize=11)
            axes[0].tick_params(axis='x',labelrotation=20,labelsize=8)
            import matplotlib.dates as mdates
            axes[0].xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
            axes[0].xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=6))
            centers=[];weights=[]
            for row in rows:
                centers.extend((np.array(row['edges'][:-1])+np.array(row['edges'][1:]))/2)
                weights.extend(row['hist'])
            # Distribution rebinning is approximate; Spec out count is exact from source values.
            _,hist_edges,bars=axes[1].hist(centers,weights=weights,bins=32,color='#7797b5',edgecolor='white')
            for left,right,bar in zip(hist_edges[:-1],hist_edges[1:],bars):
                center=(left+right)/2
                if (latest['low'] is not None and center<latest['low']) or (latest['high'] is not None and center>latest['high']):
                    bar.set_facecolor('#b4232d')
            axes[1].set_ylabel('Count')
            pct=finding['out_pct'];pct_text='N/A' if pct is None else f'{pct:.2f}%'
            axes[1].set_title(f'Value distribution · Spec out {pct_text}',fontsize=11)
            for ax in axes:
                if latest['low'] is not None:ax.axhline(latest['low'],color='#bb2222',ls='--',lw=1) if ax==axes[0] else ax.axvline(latest['low'],color='#bb2222',ls='--',lw=1)
                if latest['high'] is not None:ax.axhline(latest['high'],color='#bb2222',ls='--',lw=1) if ax==axes[0] else ax.axvline(latest['high'],color='#bb2222',ls='--',lw=1)
                ax.grid(alpha=.2)
            raw=io.BytesIO();fig.savefig(raw,format='jpg',dpi=dpi,pil_kwargs={'quality':75});plt.close(fig)
            slide.shapes.add_picture(io.BytesIO(raw.getvalue()),Inches(.35),Inches(1.7),width=Inches(12.2))
            title(slide,f"{finding['lots']} lots / n={finding['n']} / {latest['aggregation']} / "
                        f"program={latest['program']} / temp={latest['temperature']} / {latest['mode']}",6.2,11)
            title(slide,f'PPT {index} · 점수=Spec pass 비율; 분포 그림은 요약 bin을 재구성, Spec out 수치는 원자료에서 계산',6.7,10)
        output=io.BytesIO();prs.save(output)
        if output.tell()<10_000_000:return output.getvalue()
    raise ValueError('Watchdog PPT 10MB 미만 압축 실패')


def _watchdog_publication(measurement, report, now, settings):
    """Use the exact measurement revision for both rollups and detail rows."""
    if report and str(report.get('tkout_time'))!=str(measurement.get('tkout_time')):report={}
    status=report.get('status','');email=report.get('email','');reason=report.get('reason') or measurement.get('reason','')
    if email=='unknown' or status=='unknown':
        return report,'발송 확인 필요','메일 수신·서버 이력 확인 후 재발송 여부 결정',True
    if status=='queued':
        return report,'발행 대기','Auto Report 다음 처리와 Scheduler 실행 상태 확인',True
    if status=='running':
        stale=now-report.get('updated',report.get('started',now))>float(settings.get('progress_stale_sec',settings.get('stale_sec',180)))
        return report,('처리 지연' if stale else '진행 중'),('Main 마지막 단계와 실행 로그 확인' if stale else '현재 실행 완료 대기'),stale
    if status=='failed' or email in ('failed','retryable'):
        return report,'발행 실패','실패 단계와 재시도 이력 확인',True
    if report.get('generated') and report.get('saved'):
        if email=='sent':return report,'발행 완료','추가 조치 없음',False
        if email=='disabled':return report,'저장 완료 · 발송 꺼짐','메일 사용 설정에 따른 미발송',False
        return report,'저장 완료 · 메일 확인','메일 사용 설정과 전송 결과 확인',True
    if not report and any(token in reason for token in ('=False','=True','제외','조건 미충족')):
        return report,'설정 제외 / 측정 대기','제외 설정 또는 측정 완료 조건 확인',False
    return report,'발행 이력 미확인','최신 측정의 발행 대상 여부와 실행 로그 확인',True


def _watchdog_report(request):
    import html
    settings=request['settings']
    if request.get('send') and not settings.get('recipients'):
        return dict(status='disabled',reason='지정 수신처 없음; 발행 생략')
    vehicle=settings.get('mail_vehicle')
    if not vehicle:raise ValueError('watchdog.mail_vehicle 설정 필요')
    GLOBAL_CONFIG.load_from_yaml(vehicle)
    start=float(request.get('window_start',time.time()-86400)); now=float(request.get('now',time.time()))
    reports=[r for r in ops_list('reports') if r.get('started',0)<=now]
    measurements=ops_list('measurements');runs=[r for r in ops_list('runs',start) if r.get('started',0)<=now]
    attempts=[r for r in ops_list('report_attempts',start) if r.get('started',0)<=now]
    health=request['health']
    report_by_pk={}
    for row in sorted(reports,key=lambda r:r.get('started',0)):
        report_by_pk[(row['prime_key'],str(row.get('tkout_time')))]=row
    new=[m for m in measurements if start<=m.get('revision_seen',m.get('first_seen',0))<=now]
    # Include every prime key checked in the window, even when no new report was due.
    relevant={m['prime_key']:m for m in measurements if start<=m.get('last_seen',0)<=now}
    for r in reports:
        if start<=r.get('started',0)<=now:
            relevant.setdefault(r['prime_key'],dict(prime_key=r['prime_key'],vehicle=r['vehicle'],lot=r['lot'],step=r['step'],tkout_time=r.get('tkout_time',''),reason=''))
    def esc(value):return html.escape(str(value))
    def table(headers,rows):return _service_table(headers,rows)
    summary=f"{health['message']} / 확인 {len(relevant)} prime keys / 신규·갱신 측정 {len(new)} prime keys"
    body=[_service_html_start('Auto Report · Watchdog','운영 담당자용 · Scheduler 실행, 최신 측정의 생성·저장·메일 결과 확인',
          '집계: '+str(datetime.fromtimestamp(start))+' ~ '+str(datetime.fromtimestamp(now))+' (운영 서버 시각)'),
          '<p><strong>'+esc(health['message'])+'</strong> · '+esc(health.get('detail',''))+'</p>']
    import json,glob
    scheduler_runs=[]
    for path in glob.glob(os.path.join(operations_root(),'scheduler_runs','*.json')):
        try:
            with open(path,encoding='utf-8') as stream:record=json.load(stream)
            if start<=record.get('finished',0)<=now:scheduler_runs.append(record)
        except (OSError,ValueError):continue
    publication={pk:_watchdog_publication(m,report_by_pk.get((pk,str(m.get('tkout_time'))),{}),now,settings) for pk,m in relevant.items()}
    action_rows=[]
    if health.get('state')!='healthy':action_rows.append(['Scheduler',health['message'],health.get('detail',''),'Scheduler 프로세스와 마지막 단계 로그 확인'])
    for pk,(r,status,action,attention) in sorted(publication.items()):
        if attention:action_rows.append([relevant[pk].get('vehicle','')+' / '+pk,status,r.get('reason') or relevant[pk].get('reason',''),action])
    for r in scheduler_runs:
        if r.get('rc')!=0:action_rows.append([r.get('vehicle','')+' / '+r.get('id',''),'실행 실패','종료 코드 '+str(r.get('rc')),'해당 Run ID의 실행 로그 확인'])
    service_rows=[]
    for service,label in [('daily_trend','Daily Trend'),('mlmode','ML mode')]:
        history=[r for r in ops_list(service+'_reports') if start<=r.get('created',0)<=now
                 and not r.get('preview_only') and r.get('status')!='preview']
        for record in sorted(history,key=lambda r:r.get('created',0)):
            state=record.get('status','unknown')
            service_rows.append([label,datetime.fromtimestamp(record['created']).isoformat(timespec='minutes'),state,record.get('items',0),record.get('manifest','')])
            if state in ('failed','unknown','insufficient_data') or record.get('notification_only'):
                action_rows.append([label,state,'분석·발행 결과 '+record.get('id',''),'자료 범위와 발행 manifest 확인'])
    body.append(_service_metrics([('확인 Prime key',len(relevant),'#003366'),('신규·갱신 측정',len(new),'#003366'),
                 ('최신 측정 저장 완료',sum(bool(r.get('generated') and r.get('saved')) for r,_,_,_ in publication.values()),'#003366'),
                 ('확인 필요 항목',len(action_rows),'#b4232d' if action_rows else '#003366')]))
    body.append('<p>Heartbeat와 발행 결과는 별도 상태입니다. 설정 제외·측정 대기는 실패로 세지 않으며, 발송 확인 필요 건은 수신 여부 확인 후 재발송하세요.</p>')
    body.append(_service_heading('우선 확인 · 운영 조치','actions')+table(['제품 / 대상','상태','근거','다음 확인'],action_rows))
    body.append('<p><a href="#products" style="color:#0055aa">제품별 현황</a> &nbsp; <a href="#publications" style="color:#0055aa">최신 측정 발행 결과</a> &nbsp; <a href="#execution" style="color:#0055aa">실행 로그</a></p>')
    product_rows=[]
    for product in sorted(set(settings.get('products',[])) | {r.get('vehicle','') for r in runs} | {r.get('vehicle','') for r in scheduler_runs} | {r.get('vehicle','') for r in relevant.values()}):
        checked=[m for m in relevant.values() if m.get('vehicle')==product]
        executions=[r for r in scheduler_runs if r.get('vehicle')==product]
        failures=sum(r.get('rc')!=0 for r in executions)
        status='실패 확인' if failures else ('실행 완료 확인' if executions else '구간 내 완료 이력 없음')
        checked_reports=[publication[m['prime_key']][0] for m in checked]
        generated=sum(bool(r.get('generated')) for r in checked_reports)
        product_rows.append([product,status,len(executions),failures,len(checked),generated,
                             sum(bool(r.get('saved')) for r in checked_reports),sum(r.get('email')=='sent' for r in checked_reports),
                             sum(publication[m['prime_key']][3] for m in checked)])
    body+=[_service_heading('제품별 실행 및 최신 측정 발행 현황','products'),table(['제품','실행 상태','완료 횟수','실패 횟수','확인 대상','생성','저장','메일 성공','확인 필요'],product_rows)]
    body+=[_service_heading('Daily Trend / ML mode 발행 이력'),table(['서비스','분석 시각','결과','후보 / 항목 수','Manifest'],service_rows),
           '<p>이력 없음은 미사용·발행 시각 전·실행 실패 등을 구분할 수 없는 상태입니다. insufficient_data는 검정 가능한 자료 부족이며 정상 판정이 아닙니다.</p>']
    execution_body=[_service_heading('Scheduler 실행 로그별 소요 시간','execution'),table(['제품','Run ID','시작','종료','소요 시간','종료 코드','로그 파일'],
            [[r.get('vehicle',''),r.get('id',''),datetime.fromtimestamp(r['started']).isoformat(timespec='seconds'),
              datetime.fromtimestamp(r['finished']).isoformat(timespec='seconds'),
              f"{r.get('elapsed',max(0,r['finished']-r['started'])):.3f}s",r.get('rc'),
              os.path.join(operations_root(),'scheduler_runs',r.get('id','')+'.json')]
             for r in sorted(scheduler_runs,key=lambda r:r.get('started',0))])]
    rows=[]
    for pk,m in sorted(relevant.items()):
        r,status,action,attention=publication[pk]
        reason=r.get('reason') or m.get('reason','')
        rows.append([m.get('vehicle',''),pk,m.get('lot',''),m.get('step',''),m.get('tkout_time',''),
                     '성공' if r.get('generated') else '미완료','성공' if r.get('saved') else '미완료',
                     r.get('email','미발송'),r.get('attempts',0),f"{r.get('elapsed',0):.1f}s",status,reason,
                     ' / '.join(f'{k}:{v:.3f}s' for k,v in r.get('timings',{}).items()),
                     datetime.fromtimestamp(m['last_seen']).isoformat(timespec='seconds') if m.get('last_seen') else '',
                     m.get('run_id',''),m.get('log_path','')])
    headers=['제품','Prime key','Lot','Step','측정시각','생성','저장','메일','시도','시간','상태','사유','단계별 소요','로그 확인시각','Run ID','로그 파일']
    body+=[_service_heading('최신 측정별 발행 결과','publications'),table(['제품','Lot / Step','측정시각','생성 / 저장','메일','상태','사유'],
           [[r[0],r[2]+' / '+r[3],r[4],r[5]+' / '+r[6],r[7],r[10],r[11]] for r in rows]),
           '<p>전체 Prime key, 시도 횟수, 단계별 시간, Run ID와 로그 경로는 첨부 CSV에 보존합니다.</p>']
    body+=execution_body
    counts={name:sum(r.get(name) is True for r in reports if start<=r.get('started',0)<=now) for name in ('generated','saved')}
    sent=sum(r.get('email')=='sent' for r in reports if start<=r.get('started',0)<=now)
    body.append(f'<p>기간 내 발행 이력 전체 (과거 측정·수동 발행 포함): 생성 {counts["generated"]} / 저장 {counts["saved"]} / 메일 성공 {sent}건. 상단 최신 측정 집계와 범위가 다르며 재시도 횟수는 별도 표기합니다.</p>')
    from collections import defaultdict
    by_key=defaultdict(list)
    for attempt in attempts:by_key[(attempt['prime_key'],attempt.get('mode','AUTO'))].append(attempt)
    attempt_rows=[]
    for (pk,mode),items in sorted(by_key.items()):
        total=len(items)
        attempt_rows.append([pk,mode,total,f"{sum(bool(i.get('generated')) for i in items)}/{total}",
                             f"{sum(bool(i.get('saved')) for i in items)}/{total}",f"{sum(i.get('email')=='sent' for i in items)}/{total}",
                             f"{sum(i.get('elapsed',0) for i in items)/total:.1f}s",
                             ' / '.join(sorted({i.get('reason','') for i in items if i.get('reason')}))])
    body+=[_service_heading('Prime key별 발행 시도 집계'),table(['Prime key','모드','시도','생성 확인','저장 확인','메일 성공','평균 시간','사유'],attempt_rows)]
    body.append('<p>생성·저장 확인은 각 시도에서 유효 산출물이 확보되었는지를 뜻하며, 저장 파일 재사용 재시도도 포함합니다.</p>')
    body+=[_service_heading('제품 실행 속도 / 실패'),table(['제품','Run ID','상태','총 시간','단계별 시간','로그 파일','오류'],
            [[r.get('vehicle'),r.get('id',''),r.get('status'),f"{r.get('elapsed',now-r.get('started',now)):.3f}s",
              ' / '.join(f'{k}:{v:.3f}s' for k,v in r.get('timings',{}).items()),
              os.path.join(operations_root(),'progress',r.get('id','')+'.json'),r.get('error','') or '; '.join(r.get('issues',[]))] for r in runs])]
    body.append('<p>Heartbeat는 현재 loop 응답 여부입니다. 위 실행 실패·미발행·미확인 표를 함께 확인하세요. 전체 Category Trend와 Spec 이상 분석은 별도 Daily Trend 메일에서 제공합니다.</p>')
    body.append('</div></body></html>');content=''.join(body)
    dest=os.path.join(operations_root(),'watchdog');os.makedirs(dest,exist_ok=True)
    identity=request['id'];safe=re.sub(r'[^A-Za-z0-9_.-]','_',identity)
    hp=os.path.join(dest,safe+'.html');pp=os.path.join(dest,safe+'.csv')
    atomic_bytes(hp,content.encode('utf-8'))
    atomic_bytes(pp,pd.DataFrame(rows,columns=headers).to_csv(index=False).encode('utf-8-sig'))
    result=dict(id=identity,html=hp,csv=pp,checked_prime_keys=len(rows),new_measurements=len(new),attention_count=len(action_rows),status='preview')
    if request.get('send'):
        recipients=settings.get('recipients') or []
        if not recipients:raise ValueError('Watchdog 수신처 미설정')
        # Watchdog delivery is independently enabled by scheduler.yaml.
        addresses={}
        for recipient in recipients:
            for receiver in _trigger_receivers(GLOBAL_CONFIG.get('email_list_path'),recipient):addresses[receiver['email'].lower()]=receiver
        if not addresses:raise ValueError('Watchdog 수신처 미설정')
        receivers=[dict(r,seq=i) for i,r in enumerate(addresses.values(),1)]
        states=[_durable_mail('watchdog|'+identity,receivers,
                '[Auto Report Watchdog] 확인 필요 '+str(len(action_rows))+'건 / '+health['message'],hp,pp,GLOBAL_CONFIG)]
        result['status']='sent' if all(v=='sent' for v in states) else ('unknown' if 'unknown' in states else 'failed')
    ops_put('watchdog_reports',identity,result)
    return result


def main():
    global _RUN
    argument=sys.argv[1] if len(sys.argv)>1 else ''
    if argument=='--mlmode-evaluate':
        destination=os.path.join(operations_root(),'mlmode_evaluation',datetime.now().strftime('%Y%m%d-%H%M%S'))
        report=mlmode_evaluate(dict(GLOBAL_CONFIG.mlmode),destination)
        print(f"[INFO] ML Lab 가상 데이터 검증 완료: {destination} / {report['ensemble']}")
        return 0
    if argument=='--daily-trend-report':
        import json
        with open(sys.argv[2],encoding='utf-8') as stream:request=json.load(stream)
        result=_daily_trend_report(request)
        return 0 if result['status'] in ('sent','preview','no_findings','insufficient_data','disabled') else 1
    if argument=='--watchdog-report':
        import json
        with open(sys.argv[2],encoding='utf-8') as stream:request=json.load(stream)
        result=_watchdog_report(request)
        return 0 if result['status'] in ('sent','preview','disabled') else 1
    if argument.startswith('--'):
        return _main_impl()
    _RUN=OperationRun(argument)
    try:
        _,vehicle,_,_,_=_parse_trigger(argument)
        lock_name=re.sub(r'[^A-Za-z0-9_.-]','_',vehicle)
        with process_lock(os.path.join(operations_root(),'locks',lock_name+'.lock')):
            _main_impl()
            return _RUN.finish()
    except BaseException as exc:
        _RUN.finish(exc)
        raise
    finally:
        shutdown_chart_pool()


if __name__ == "__main__":
    sys.exit(main() or 0)
