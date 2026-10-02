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
# 기본 설치는 보조 모듈을 ZIP에서 읽는다. 추출한 개발 소스가 있으면 그것이 우선한다.
_runtime_zip = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auto_report_runtime.zip')
if os.path.isfile(_runtime_zip) and _runtime_zip not in sys.path:
    sys.path.append(_runtime_zip)
# NOTE: bigdataquery는 Main에서 직접 쓰지 않으므로 import하지 않는다.
#   (병렬 렌더링 워커가 __main__=Main을 재import할 때 무거운 bigdataquery 재import·안내문
#    출력이 매번 발생하던 문제 방지 — 실제 쿼리는 My_Function 내부에서 지연 import한다.)
from My_Function import *
from My_Function import _filter_inline_by_vehicle, _et_completion_frame  # import * 는 언더스코어 이름 미포함
from My_config import GLOBAL_CONFIG
from anomaly_engine import analyze_commonality, render_findings_html, item_excluded
from operator_console import STAGES, color

_CODE_VERSION = None

# 측정값 기반 통계 판정

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
    from operator_console import color as console_color
    return console_color(text, {'green':'ok','red':'error','yellow':'warn'}.get(color,'info'))

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
    tag, color = {'ok': ('[완료]', 'green'), 'fail': ('[실패]', 'red'), 'info': ('[진행]', 'blue'),
                  'skip': ('[건너뜀]', 'yellow'), 'on': ('[사용]', 'green'), 'off': ('[사용 안 함]', 'yellow')
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


def _save_archive_snapshot(report_key, meta, findings, item_stats,
                           target_rows=None, index_items=None, rule_trace=None):
    """발행 스냅샷을 RUN/ARCHIVE/<report_key>/에 저장.

    - summary.json        : 발행 메타(generated_at 포함) + findings + item_stats.
    - target_rows.parquet : target lot 측정 rows 중 '발행 당시 REPORT ORDER index' 컬럼만(+좌표 메타)
                            — 이후 reformatter/ADDP가 바뀌어도 당시 값이 고정 보존.
    스냅샷은 부가 산출물: 읽는 기능은 파일이 지워져 있어도 동작해야 하고, 저장 실패도
    리포트 발행에 영향을 주지 않는다(호출부 try/except).
    rule_trace 인자는 과거 호환용으로만 유지하며 저장하지 않는다.
    """
    import json as _json
    _dir = os.path.join('RUN', 'ARCHIVE', re.sub(r'[^0-9A-Za-z가-힣._-]+', '_', str(report_key)))
    os.makedirs(_dir, exist_ok=True)
    with open(os.path.join(_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        _json.dump({**meta, 'findings': findings, 'item_stats': item_stats}, f, ensure_ascii=False, indent=2, default=str)
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

        # Inline Table 멀티 인덱스 (UCL 앞 4열: Module / Step desc / ITEMNAME / Item)
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
    # Flat chart/map backgrounds compress well as palette PNG, without resizing
    # labels or blurring thin lines. Try this before the JPEG/resize fallback.
    for colors in (256, 128, 64):
        _b = _ioc.BytesIO()
        _im.quantize(colors=colors, method=_PILc.Quantize.MEDIANCUT,
                     dither=_PILc.Dither.NONE).save(_b, 'PNG', optimize=True)
        if _b.tell() <= _budget:
            return 'data:image/png;base64,' + base64.b64encode(_b.getvalue()).decode('utf-8')
    for attempt in range(24):
        scale = 0.8 ** attempt
        resized = _im.resize((max(1, int(_im.width * scale)), max(1, int(_im.height * scale))), _PILc.Resampling.LANCZOS)
        _b = _ioc.BytesIO()
        resized.save(_b, format='JPEG', quality=max(20, 80 - attempt * 5), optimize=True)
        if _b.tell() <= _budget:
            return 'data:image/jpeg;base64,' + base64.b64encode(_b.getvalue()).decode('utf-8')
    raise ValueError('인라인 이미지 크기 제한 초과')


def _html_mail_limit(config=None, settings=None):
    """Decimal MB, including UTF-8 text and inline base64; old 2MB settings cannot override 1MB."""
    config = GLOBAL_CONFIG if config is None else config
    configured = int(float(config.get('html_mail_max_mb', 1.0) or 1.0) * 1_000_000)
    service = int((settings or {}).get('html_max_bytes', 1_000_000))
    limit = min(1_000_000, configured, service)
    if limit <= 0:
        raise ValueError('메일 HTML 용량 한도는 양수여야 합니다')
    return limit


def _compact_report_html(content):
    """Shorten generated inline CSS and indentation, keeping visible text and mail-safe styles."""
    def style(match):
        value = re.sub(r'\s*([:;,])\s*', r'\1', match.group(2)).strip().rstrip(';')
        value = re.sub(r'#([0-9a-f])\1([0-9a-f])\2([0-9a-f])\3\b',
                       r'#\1\2\3', value, flags=re.I)
        return match.group(1) + value + match.group(3)
    # Do not alter whitespace in preformatted text, scripts, CSS or textarea values.
    parts = re.split(r'(<(?:pre|script|style|textarea)\b[^>]*>.*?</(?:pre|script|style|textarea)\s*>)',
                     content, flags=re.I | re.S)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'(\bstyle=")([^"]*)(")', style, parts[i])
        parts[i] = re.sub(r'>[ \t]*[\r\n]+\s*<', '><', parts[i])
    return ''.join(parts)


def _fit_html_budget(content, config=None, settings=None):
    """Keep every table value and image; compact markup first, then fit images to the remaining bytes."""
    config = GLOBAL_CONFIG if config is None else config
    limit = _html_mail_limit(config, settings)
    before = len(content.encode('utf-8'))
    content = _compact_report_html(content)
    # Reserve 5% for mail processing; the hard boundary remains the complete UTF-8 body.
    target = int(limit * .95)
    pattern = r'(<img\b[^>]*?\bsrc=")(data:image/[^"\s]+)(")'
    for attempt in range(8):
        size = len(content.encode('utf-8'))
        if size <= target:
            break
        matches = list(re.finditer(pattern, content, flags=re.I | re.S))
        image_bytes = sum(len(m.group(2)) for m in matches)
        text_bytes = size - image_bytes
        if not matches and size < limit:
            break
        if text_bytes >= limit or not matches:
            raise ValueError(f'메일 본문 텍스트·표만 {text_bytes:,} bytes: {limit:,} bytes 한도 초과; 항목/비교 범위 분할 필요')
        # When text leaves less than the reserved margin, use the hard boundary.
        available = max(target - text_bytes, int((limit - text_bytes) * .95))
        ratio = min(.95, available / image_bytes * .96)
        cache = {}
        def shrink(match):
            uri = match.group(2)
            if uri not in cache:
                raw = base64.b64decode(uri.split(',', 1)[1], validate=True)
                kb = max(1, int((len(uri) * ratio - 30) * .75 / 1024))
                kb = min(kb, int(config.get('html_inline_img_max_kb', 100) or 100))
                packed = _img_datauri(raw, max_kb=kb)
                cache[uri] = packed if len(packed) < len(uri) else uri
            return match.group(1) + cache[uri] + match.group(3)
        reduced = re.sub(pattern, shrink, content, flags=re.I | re.S)
        if reduced == content:
            break
        content = reduced
    after = len(content.encode('utf-8'))
    if after >= limit:
        raise ValueError(f'메일 본문 {after:,} bytes > 한도 {limit:,} bytes; 항목/비교 범위 분할 필요')
    print(f'[INFO] HTML 용량 {before:,} -> {after:,} bytes / 한도 {limit:,} bytes')
    return content


def _render_score_board(frame, target_lot, display_name=str):
    """Render all item/wafer values with mail-safe inline styles and inherited row typography."""
    sb_rows = list(frame.iterrows())
    _wcols = list(frame.columns)

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
    _SB_WAF = (f'{_SB_BD} width:{_sb_waf_w}px; min-width:{_sb_waf_w}px; '
               f'max-width:{_sb_waf_w}px; padding:3px 1px; font-size:11px;')
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

    sb_html += '<table class="score-board" style="border-collapse:collapse; font-size:11px; text-align:center; white-space:nowrap; font-variant-numeric:tabular-nums;">\n  <thead>\n'
    sb_html += '    <tr>\n'
    sb_html += f'      <th colspan="2" class="sb-frozen-lot" style="{_SB_BD} {_SB_PAD} text-align:center; background-color:#d9e1f2;">LOT_ID</th>\n'
    # root_lot_id가 같은 형제 lot을 각각 헤더로 분리 (target lot은 강조)
    for _lot, _cols in _lot_groups:
        _is_tgt = (str(_lot) == str(target_lot))
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
        sb_html += '    <tr style="font-weight:bold; text-align:center; white-space:nowrap; font-variant-numeric:tabular-nums;">\n'
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
                sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:{bg_color}; color:{color};">{val:.1f}</td>\n'
        sb_html += '    </tr>\n'
    sb_html += '  </tbody>\n</table>\n'

    return _compact_report_html(sb_html)


def _parse_trigger(argument):
    """Report, DB_SETTING or WIP_SYNC TRIGGER; leading underscore optional."""
    value = argument[1:] if argument.startswith('_') else argument
    if not value.startswith('TRIGGER_'):
        return None, argument, None, None, None
    value = value[len('TRIGGER_'):]
    if value.startswith('WIP_SYNC_'):
        vehicle = value[len('WIP_SYNC_'):].strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', vehicle):
            raise ValueError('WIP 갱신 형식: _TRIGGER_WIP_SYNC_<vehicle>')
        return 'WIP_SYNC', vehicle, None, None, None
    if value.startswith('DB_SETTING_'):
        vehicle = value[len('DB_SETTING_'):].strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', vehicle):
            raise ValueError('DB setting 형식: _TRIGGER_DB_SETTING_<vehicle> --days N --parallel N')
        return 'DB_SETTING', vehicle, None, None, None
    mode = 'TRIGGER'
    for candidate in ('FORCE', 'NORMAL', 'ALL', 'SINGLE'):
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


_UPLOAD_POOL = None
_UPLOADS = []   # [(search_key, report record, Future)]


def _upload_async(client, local_path, bucket, key, record, search_key):
    """S3 전송을 백그라운드 스레드로. 실행 이력(record)은 메인 스레드의 _drain_uploads 만 고친다."""
    global _UPLOAD_POOL
    if _UPLOAD_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _UPLOAD_POOL = ThreadPoolExecutor(max_workers=max(1, int(GLOBAL_CONFIG.get('s3_upload_threads', 2) or 2)),
                                          thread_name_prefix='s3-upload')

    def job():
        started = time.time()
        client.upload_file(local_path, bucket, key)
        return f'{bucket}/{key} ({time.time() - started:.1f}s)'
    _UPLOADS.append((search_key, record, _UPLOAD_POOL.submit(job)))


def _drain_uploads(block=True):
    """끝난(또는 block=True 면 전부) S3 전송 결과를 실행 이력에 반영."""
    remaining = []
    for search_key, record, future in _UPLOADS:
        if not block and not future.done():
            remaining.append((search_key, record, future))
            continue
        try:
            detail = future.result()
            record['upload'] = 'success'
            print_status("S3 업로드", "ok", detail)
        except Exception as exc:
            record['upload'] = 'failed'
            if _RUN:
                _RUN.data["issues"].append(f"S3 업로드 실패: {search_key}")
            print_status("S3 업로드", "fail", f"{search_key}: {exc}")
        record['updated'] = time.time()
        if record.get('id'):
            ops_put('reports', record['id'], record)
    _UPLOADS[:] = remaining


def _release_memory():
    try:
        import resource_governor
        resource_governor.release_memory()
    except Exception:
        gc.collect()


def _force_viewing_period_all(log_frame, lots, steps, days, now):
    """여러 대상의 FORCE 기간 = 가장 오래된 대상 기준. 로그에 없는 대상은 경고만 하고 건너뛴다."""
    found, missing = [], []
    for lot, step in trigger_pairs(lots, steps):
        try:
            found.append(_force_viewing_period(log_frame, lot, step, days, now))
        except ValueError:
            missing.append(f'{lot}_{step}')
    if missing:
        print(f'[WARN] FORCE: 측정 로그에 없는 대상 {len(missing)}건은 기간 계산에서 제외: {missing[:5]}')
    if not found:
        raise ValueError(f'FORCE: {missing[:3]} prime key 진행날짜를 log에서 찾지 못했습니다')
    return max(found)


def _filter_normal_shots(frame, zones):
    flag = next((c for c in zones if str(c).strip().lower() == '13pt'), None)
    if flag is None:
        raise ValueError('NORMAL: Extractor Zone_Define에 13pt 열이 없습니다')
    keys = ['MASK', 'CHIP_X_POS', 'CHIP_Y_POS', 'FLAT_ZONE_POS']
    allowed = zones.loc[zones[flag].astype(str).str.strip().str.lower().isin(['o', 'y', 'true']), keys].drop_duplicates()
    left = ['mask', 'chip_x_pos', 'chip_y_pos', 'flat_zone']
    return frame.merge(allowed.rename(columns=dict(zip(keys, left))), on=left, how='inner', validate='many_to_one')


def _trigger_receivers(path, recipient):
    """직접 주소는 엑셀 없이, 그룹명은 지정된 시트의 명단으로 발송한다."""
    if '@' in recipient:
        return email_receivers(recipient.split(','))
    return get_email_list(path, recipient, default_group=None)


def _all_trend_sheets(tiles, columns=2, width=1280):
    """ALL Trends 메일용 — 차트들을 2열 시트 이미지로 합쳐 메일 1통의 이미지 수를 _mail_image_limit 이하로 맞춘다.
    각 칸 위에 항목명(ASCII)을 그려 넣어 어떤 차트인지 이미지 안에서도 읽힌다."""
    import io
    import math
    from PIL import Image, ImageDraw, ImageFont
    limit = _mail_image_limit()
    per_sheet = max(columns * 3, math.ceil(len(tiles) / limit))
    per_sheet += (-per_sheet) % columns
    cell = width // columns
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 15)
    except Exception:
        try:
            font = ImageFont.truetype('arial.ttf', 15)
        except Exception:
            font = ImageFont.load_default()
    sheets = []
    for start in range(0, len(tiles), per_sheet):
        chunk = tiles[start:start + per_sheet]
        scaled = [im.resize((cell, max(1, round(im.height * cell / im.width))), Image.Resampling.LANCZOS) for _, _, im in chunk]
        row_h = max(im.height for im in scaled) + 24
        rows = math.ceil(len(chunk) / columns)
        canvas = Image.new('RGB', (cell * columns, row_h * rows), 'white')
        draw = ImageDraw.Draw(canvas)
        for i, ((cat, name, _), im) in enumerate(zip(chunk, scaled)):
            x, y = (i % columns) * cell, (i // columns) * row_h
            draw.text((x + 6, y + 3), re.sub(r'[^\x20-\x7E]', '?', str(name))[:70], fill=(0, 51, 102), font=font)
            canvas.paste(im, (x, y + 24))
        buf = io.BytesIO()
        canvas.save(buf, 'JPEG', quality=80, optimize=True)
        sheets.append((list(dict.fromkeys(c for c, _, _ in chunk)), [n for _, n, _ in chunk], buf.getvalue()))
    return sheets


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
        tiles = []   # 메일 본문은 차트 여러 개를 합친 시트 이미지로(메일 API 첨부 개수 한도 — _mail_image_limit)
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
            tiles.append((cat, name, im))
            count += 1
        sheets = _all_trend_sheets(tiles)
        body = body[:2]
        for cats, names, png in sheets:
            body.append('<h2>' + html.escape(' / '.join(cats)) + '</h2><p style="font-size:12px;color:#555">'
                        + html.escape(', '.join(names)) + '</p><img width="1280" style="width:100%;max-width:1280px;height:auto" src="'
                        + _img_datauri(png) + '">')
        body.append('</body></html>')
        content = ''.join(body)
        ppt = io.BytesIO()
        prs.save(ppt)
        content = _fit_html_budget(content)
        if len(content.encode('utf-8')) < _html_mail_limit() and ppt.tell() < 10_000_000:
            _assert_inline_images(content, len(sheets))
            print('[INFO] HTML 인라인 이미지 검증 OK')
            return content, ppt.getvalue()
    raise ValueError('ALL: 모든 trend를 유지하면서 HTML 1MB/PPTX 10MB 미만으로 축소할 수 없습니다')


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
    if state not in ('sent','disabled'):raise RuntimeError(f'ALL 메일 발송 {state}')
    if _RUN and _RUN.current:_RUN.finish_report('success')
    print(f'[INFO] ALL {len(charts)} trends: HTML {len(content.encode("utf-8"))} bytes / PPTX {len(ppt)} bytes')


_RUN = None


class OperationRun:
    def __init__(self, argument):
        self.id = os.getenv('AUTO_REPORT_RUN_ID') or uuid.uuid4().hex
        self.started = time.time()
        self.stage_started = time.perf_counter()
        self.data = dict(id=self.id, argument=argument, started=self.started, status='running',
                         vehicle='', stage='startup', timings={}, reports=[], issues=[], code_version=_CODE_VERSION)
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
        if previous != name:
            target = self.current or self.data
            identity = ' / '.join(str(target.get(k)) for k in ('vehicle','lot','step') if target.get(k))
            print_status(STAGES.get(name, name), 'info',
                         (identity + ' · ' if identity else '') + f'이전 단계 {elapsed:.1f}초')

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
                          reason='',timings={},paths={},code_version=_CODE_VERSION)
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


def _exclude_db_setup_history(candidates, vehicle):
    """Suppress AUTO revisions handled by DB setup/WIP sync; manual triggers remain available."""
    baseline = ops_get('db_setting_baseline', vehicle, {}).get('revisions', {})
    if candidates.empty or not baseline:
        return candidates
    keys = vehicle + '_' + candidates['lot_id'].astype(str) + '_' + candidates['dc_step_id'].astype(str)
    initialized = pd.to_datetime(keys.map(baseline), errors='coerce')
    measured = pd.to_datetime(candidates['tkout_time'], errors='coerce')
    return candidates.loc[initialized.isna() | measured.gt(initialized)].copy()


def _retry_candidates(candidates, final_log, vehicle):
    """Called under the product lock: queued/running records belong to interrupted work."""
    revisions={}
    for record in ops_list('reports'):
        if record.get('vehicle')!=vehicle or record.get('mode')!='AUTO':continue
        retry=(record.get('status') in ('queued','running','failed','unknown') or
               (record.get('email')=='disabled' and GLOBAL_CONFIG.get('use_email_send',False)))
        if not retry or record.get('attempts',0)>=int(GLOBAL_CONFIG.get('report_max_attempts',3)):continue
        revisions[(record['prime_key'],record['tkout_time'])]=None
    if not revisions:return _exclude_db_setup_history(candidates, vehicle)
    # Series.astype(str) drops 00:00:00 for all-midnight batches, unlike the ledger.
    keys=zip(final_log['prime_key'].astype(str),final_log['tkout_time'].map(str))
    positions={key:index for index,key in enumerate(keys)}
    matched=final_log.iloc[[positions[key] for key in revisions if key in positions]]
    matched=matched[['lot_id','dc_step_id','dc_done','tkout_time']]
    return _exclude_db_setup_history(
        pd.concat([candidates,matched],ignore_index=True).drop_duplicates(['lot_id','dc_step_id']), vehicle)


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


def _mail_attachment_guard(content, attachments, config):
    """발송 직전 최종 가드 — 본문 인라인 이미지 + 첨부 파일 수가 mail_attach_limit(기본 10)을 넘지 않게 한다.

    사내 메일 API 는 본문 data:image 를 첨부로 떼어 세는 경우가 있어 합계가 10을 넘으면
    'Attach file count is over 10' 으로 메일 전체를 거부한다(2026-07 실제 발생). 발행물(Daily/ML/Auto Report)은
    만들 때 이미 이미지 수를 맞추므로 보통은 아무것도 바꾸지 않는다. 그래도 넘으면 메일 본문의 뒤쪽 이미지를
    안내 문구로 바꿔 발송이 실패하지 않게 한다(저장된 HTML·첨부 PPT 에는 그림이 그대로 있다).
    반환: (발송할 본문, 원래 이미지 수)
    """
    pattern=r'<img\s[^>]*src="data:image/[^"]*"[^>]*>'
    images=re.findall(pattern,content,re.DOTALL)
    limit=int(config.get('mail_attach_limit',10) or 10)
    allowed=max(0,limit-attachments)
    if len(images)<=allowed:return content,len(images)
    note=('<div style="font-size:12px;color:#737373;border:1px dashed #a3a3a3;padding:6px 10px;margin:4px 0">'
          '그림 생략 — 메일 첨부 개수 한도('+str(limit)+'개) 때문에 본문에서 뺐습니다. 첨부 PPT 에 모두 있습니다.</div>')
    drop=len(images)-allowed
    for tag in reversed(images[-drop:]):
        at=content.rfind(tag)
        if at>=0:content=content[:at]+note+content[at+len(tag):]
    print(f'[WARN] 메일 본문 이미지 {len(images)}장 + 첨부 {attachments}개 > 한도 {limit}개 — 뒤쪽 이미지 {drop}장을 안내 문구로 바꿔 발송')
    return content,len(images)


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
        content,record['inline_images']=_mail_attachment_guard(content,1 if ppt_path else 0,config)
        content = _fit_html_budget(content, config)
        record['html_bytes'] = len(content.encode('utf-8'))
        record['html_limit'] = _html_mail_limit(config)
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
        if code!=200:
            # 메일 API의 거부 사유(예: 'Attach file count is over 10')를 운영 이력에 남긴다.
            try:record['reason']+=' '+re.sub(r'\s+',' ',str(response.text or ''))[:300]
            except Exception:pass
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


def _parse_command(arguments):
    """기본 실행, 초기 DB 적재, 개인 발송 명령을 기존 리포트 흐름으로 연결한다."""
    import argparse
    parser = argparse.ArgumentParser(description='Auto Report: DB 초기 적재 및 지정 수신처 발행')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--init-db', metavar='VEHICLE', help='DB setting 적재 (기본 200일, 리포트/메일 없음)')
    action.add_argument('--sync-wip', metavar='VEHICLE', help='WIP 조회 후 Final ET log 완료 상태만 갱신 (발행 없음)')
    action.add_argument('--send-user', metavar='USER', help='엑셀을 읽지 않고 USER@samsung.com 한 명에게만 발송')
    parser.add_argument('--prime-key', help='발행 대상 vehicle_lot_step')
    parser.add_argument('--single', action='store_true', help='viewing_period 없이 대상 lot/step ET만 조회')
    def positive_int(value):
        number = int(value)
        if number < 1:
            raise argparse.ArgumentTypeError('1 이상의 정수를 지정하세요')
        return number
    parser.add_argument('--days', type=positive_int, help='DB setting 적재 일수 (오늘 포함, 기본 db_setting_days=200)')
    parser.add_argument('--parallel', type=positive_int, help='DB setting 병렬 조회 수 상한 (기본 db_setting_parallel=1)')
    parser.add_argument('legacy', nargs='?', help='기존 vehicle 또는 _TRIGGER 명령')
    args = parser.parse_args(arguments)
    wip_trigger = None
    if args.legacy and args.legacy.lstrip('_').startswith('TRIGGER_WIP_SYNC_'):
        try:
            _, wip_trigger, _, _, _ = _parse_trigger(args.legacy)
        except ValueError as exc:
            parser.error(str(exc))
    if args.sync_wip is not None or wip_trigger is not None:
        if ((args.sync_wip is not None and args.legacy) or args.init_db or args.send_user
                or args.prime_key or args.single or args.days is not None or args.parallel is not None):
            parser.error('--sync-wip는 다른 적재/발행 옵션 없이 제품명만 지정합니다')
        vehicle = (args.sync_wip if args.sync_wip is not None else wip_trigger).strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', vehicle) or vehicle.lstrip('_').startswith('TRIGGER_'):
            parser.error('--sync-wip에는 TRIGGER 대신 제품명을 지정합니다')
        return dict(argument=vehicle, kind='sync_wip', recipient=None)
    db_trigger = None
    if args.legacy and args.legacy.lstrip('_').startswith('TRIGGER_DB_SETTING_'):
        try:
            _, db_trigger, _, _, _ = _parse_trigger(args.legacy)
        except ValueError as exc:
            parser.error(str(exc))
    if args.init_db is not None or db_trigger is not None:
        if (args.init_db is not None and args.legacy) or args.prime_key or args.single or args.send_user:
            parser.error('--init-db는 제품명만 지정합니다')
        vehicle = (args.init_db if args.init_db is not None else db_trigger).strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', vehicle) or _parse_trigger(vehicle)[0] is not None:
            parser.error('--init-db에는 TRIGGER 대신 제품명을 지정합니다')
        command = dict(argument=vehicle, kind='init_db', recipient=None)
        for key in ('days', 'parallel'):
            if getattr(args, key) is not None:
                command[key] = getattr(args, key)
        return command
    if args.days is not None or args.parallel is not None:
        parser.error('--days/--parallel은 DB setting 적재에서만 사용합니다')
    if args.send_user is not None:
        if args.legacy or not args.prime_key:
            parser.error('지정 발송에는 --prime-key vehicle_lot_step이 필요합니다')
        try:
            recipient = samsung_email(args.send_user)
        except ValueError as exc:
            parser.error(str(exc))
        argument = '_TRIGGER_' + ('SINGLE_' if args.single else '') + args.prime_key
        mode, _, _, _, _ = _parse_trigger(argument)
        if mode != ('SINGLE' if args.single else 'TRIGGER'):
            parser.error('--prime-key에는 TRIGGER 접두어 없는 vehicle_lot_step을 지정합니다')
        return dict(argument=argument, kind='person', recipient=recipient)
    if not args.legacy or args.prime_key or args.single:
        parser.error('vehicle, TRIGGER 또는 --init-db / --sync-wip / --send-user 명령을 지정하세요')
    return dict(argument=args.legacy, kind='legacy', recipient=None)


def _apply_command_settings(command, config):
    """YAML은 변경하지 않고 이번 실행의 적재·발송 설정만 적용한다."""
    if command['kind'] == 'sync_wip':
        config.settings.update(DB_Setting_mode=False, test_mode=False, report_making=False,
                               use_email_send=False, use_s3_upload=False)
        return None
    if command['kind'] == 'init_db':
        days = command.get('days', config.get('db_setting_days', 200))
        parallel = command.get('parallel', config.get('db_setting_parallel', 1))
        if any(type(value) is not int or value < 1 for value in (days, parallel)):
            raise ValueError('db_setting_days/db_setting_parallel은 1 이상의 정수여야 합니다')
        config.settings.update(DB_Setting_mode=True, QueryTimeSpan=days, now_minus=0,
                               db_setting_parallel=parallel,
                               test_mode=False, report_making=False, use_email_send=False,
                               use_s3_upload=False, et_force_full_refresh=True)
        if config.get('SplitTimeSpan') is None:
            config.settings['SplitTimeSpan'] = 7
        print(f'[INFO] DB setting: 오늘 포함 최근 {days}일, 병렬 요청 상한 {parallel}, '
              '전체 조회, 리포트/메일/S3 비활성, executor 잠금 우회')
        return None
    if _parse_trigger(command['argument'])[0] in ('TRIGGER', 'SINGLE', 'NORMAL', 'FORCE', 'ALL'):
        # 명시한 Lot/Step 강제발행은 제품의 자동 적재/P-Type 제외 설정보다 우선한다.
        config.settings.update(DB_Setting_mode=False, ptype_lot_turnoff=False, report_making=True)
        if command['kind'] != 'person':
            print('[INFO] 보고서 TRIGGER: DB_Setting_mode=False, ptype_lot_turnoff=False, report_making=True')
    if command['kind'] != 'person':
        return None
    # parser가 user 부분에 고정 도메인을 붙였으며, 여기서도 완성 주소를 재검증한다.
    mail = normalize_email(command['recipient'])
    config.settings.update(DB_Setting_mode=False, report_making=True, use_email_send=True,
                           use_s3_upload=False, email_receiver=[mail])
    print('[INFO] 개인 발송: 입력한 이메일 1명만 사용 (메일링 엑셀 조회 없음)')
    return mail


def _main_impl(command=None):
    global _LOG_PATH

    command = command or _parse_command(sys.argv[1:])
    raw_arg = command['argument']
    trigger_mode, vehicle_name, trigger_lot, trigger_step, trigger_mail = _parse_trigger(raw_arg)
    trigger_flag = trigger_mode is not None

    # config.yaml에서 설정 로드
    GLOBAL_CONFIG.load_from_yaml(vehicle_name)
    explicit_mail = _apply_command_settings(command, GLOBAL_CONFIG)
    # 큐의 생성 전용 요청은 이 자식 프로세스에만 적용한다.
    if os.getenv('AUTO_REPORT_GENERATE_ONLY') == '1':
        GLOBAL_CONFIG.settings.update(use_email_send=False, use_s3_upload=False)
    if explicit_mail:
        trigger_mail = explicit_mail

    if command['kind'] == 'init_db':
        # 적재 전용 경로: 성공한 ET 이력은 완료 기준선으로 초기화하며 WIP/렌더링/발송은 하지 않는다.
        _LOG_PATH = GLOBAL_CONFIG.get('unified_log') or GLOBAL_CONFIG.get('loop_log')
        builtins.print = _run_log_print
        _RUN.data['vehicle'] = GLOBAL_CONFIG.get('vehicle')
        _RUN.stage('et_query')
        _RUN.data['db_setting'] = etdata_query()
        print_status('DB setting 적재', 'ok', '날짜별 원시 DB·ET 로그 반영, 기존 이력 dc_done=True 초기화 완료')
        return

    if command['kind'] == 'sync_wip':
        _LOG_PATH = GLOBAL_CONFIG.get('unified_log') or GLOBAL_CONFIG.get('loop_log')
        builtins.print = _run_log_print
        _RUN.data['vehicle'] = GLOBAL_CONFIG.get('vehicle')
        _RUN.stage('wip_query')
        wip = wipdata_query()
        _RUN.stage('measurement_selection')
        settings = {key: GLOBAL_CONFIG.get(key) for key in
                    ('vehicle', 'et_log_path', 'Final_et_log_path', 'delay_min')}
        settings['lock_wait_sec'] = GLOBAL_CONFIG.get('product_lock_wait_sec', 3600)
        _RUN.data['wip_sync'] = sync_wip_et_completion(settings, wip)
        print_status('WIP 완료 상태 갱신', 'ok', 'Final ET log 갱신·과거 자동 발행 제외, 보고서/메일/S3 없음')
        return

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
    # ET 원시는 제품별 daily DB에서 직접 조회한다.

    # RUN/TEMP = 임시 산출물 폴더
    _temp_dir = os.path.join(ROOT, 'TEMP')
    for target_path in [ROOT, DB, DB_et_daily, log, Report, low_qual_ppt_save_path, html_save_path, _temp_dir]:
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

    _ANOMALY_KNOWLEDGE_TEXT = ""
    try:
        _kp = GLOBAL_CONFIG.get("anomaly_knowledge_path")
        if _kp and os.path.exists(_kp):
            with open(_kp, encoding="utf-8") as _kf:
                _ANOMALY_KNOWLEDGE_TEXT = _kf.read()
    except Exception as _ke:
        print(f"[WARN] 이상 지식베이스 로드 실패: {_ke}")

    reformatter = pd.read_csv(f'reformatter/{vehicle}_reformatter.csv')

    reformatter_check = reformatter_verify(reformatter)
    if reformatter_check:
        print_status("Reformatter 검증", "ok", f"{vehicle}_reformatter.csv 통과")
    else:
        print_status("Reformatter 검증", "fail", f"{vehicle}_reformatter.csv 실패 → 리포트 미발행")

    if reformatter_check :
        conn = duckdb.connect()
        # DuckDB 기본값(모든 코어·RAM 80%)은 같은 서버의 다른 Main/S3 작업과 자원을 다툰다 → 남은 만큼만.
        try:
            import resource_governor
            _duck = resource_governor.duckdb_settings(GLOBAL_CONFIG)
            conn.execute(f"SET threads={int(_duck['threads'])}")
            conn.execute(f"SET memory_limit='{_duck['memory_limit']}'")
            print(f"[PERF] DuckDB threads {_duck['threads']} / memory_limit {_duck['memory_limit']}")
        except Exception as _duck_err:
            print(f"[WARN] DuckDB 자원 한도 설정 생략: {_duck_err}")

        #test_mode True일 경우 etdata_query 진행하지않고 Report 생성만 진행
        if not test_mode and not trigger_flag:
            # ── ET 데이터 쿼리 (Hive 파티션으로 daily 폴더에 저장) ──
            _RUN.stage('et_query')
            etdata_query()
            print_status('DC 측정 데이터 갱신', 'ok', '조회 결과를 제품별 DB에 반영했습니다.')
            log_to_file("Query Success...", query_log)

            _RUN.stage('wip_query')
            wipdata_query()
            print_status('공정 진행 현황 갱신', 'ok', 'Lot별 현재 공정과 DC 측정 완료 여부를 비교합니다.')

        _RUN.stage('measurement_selection')
        _et_dtype = {'prime_key': str, 'lot_id': str, 'dc_step_id': str}
        et_log = pd.read_csv(et_log_path, dtype=_et_dtype) # n일 치 et_log
        existing_lot_log = pd.read_csv(Final_et_log_path, dtype=_et_dtype) if os.path.exists(Final_et_log_path) else pd.DataFrame(columns=['prime_key','wafer_id','step_seq','total_site_cnt',\
                                                                                                                        'tkout_time','lot_id','dc_step_id','dc_done'])

        wip_current = pd.read_csv(DB + f'{vehicle}_wip_current.csv', encoding='cp949',
                                  dtype={'lot_id': str, 'step_id': str})
        final_lot_log = _et_completion_frame(existing_lot_log, et_log, wip_current, vehicle,
                                             delay_min, datetime_now)

        selected_et_log = final_lot_log[['lot_id', 'dc_step_id', 'dc_done','tkout_time']]
        selected_et_log_before = existing_lot_log[['lot_id', 'dc_step_id', 'dc_done']]
        selected_et_log_before.rename(columns={'dc_done': 'dc_done_before'}, inplace=True)
        selected_et_log = pd.merge(selected_et_log, selected_et_log_before, on=['lot_id','dc_step_id'], how='left')

        # DC 완료여부 판정 Logic
        dc_done_list = selected_et_log[(selected_et_log['dc_done'] != selected_et_log['dc_done_before'])]
        dc_done_list = dc_done_list[dc_done_list['dc_done'] == True]
        if not trigger_flag:dc_done_list=_retry_candidates(dc_done_list,final_lot_log,vehicle)

        if not trigger_flag:
            print_status('발행 후보 확인', 'info', f'신규 측정 완료 또는 재시도 대상 {len(dc_done_list)}건')

        if ptype_lot_turnoff == True or ptype_lot_turnoff == 'True' :
            dc_done_list = dc_done_list[~dc_done_list['lot_id'].str.startswith('A4')]
            print(f"[INFO] P-Type(A4*) 제외 후 LOT: {len(dc_done_list)}건")

        if specific_dc_layer is not False:
            dc_done_list['dc_layer_check'] = dc_done_list['dc_step_id'].map(GLOBAL_CONFIG.get("dc_dict"))
            dc_done_list = dc_done_list[dc_done_list['dc_layer_check'] == 'MFDC']
            dc_done_list = dc_done_list.drop(columns=['dc_layer_check'])
            print(f"[INFO] 제품에 지정된 DC Layer 조건 적용 후 발행 후보: {len(dc_done_list)}건")

        # 수동 발행은 명령에 적힌 lot·step 만 대상으로 한다(쉼표로 여러 개 — trigger_pairs 규칙).
        # 여러 Lot 을 한 번에 돌리면 DB 조회·피벗·좌표 결합을 한 번만 하고 Lot 별 리포트만 반복한다.
        if trigger_flag:
            _pairs = trigger_pairs(trigger_lot, trigger_step)
            dc_done_list = {
                'lot_id': [p[0] for p in _pairs],
                'dc_step_id': [p[1] for p in _pairs],
                'dc_done': [True] * len(_pairs),
                'dc_done_before': [False] * len(_pairs)
            }
            if len(_pairs) > 1:
                print(f'[INFO] 수동 발행 대상 {len(_pairs)}건을 한 번의 데이터 적재로 처리합니다: '
                      + ', '.join(f'{a}/{b}' for a, b in _pairs[:10]) + (' …' if len(_pairs) > 10 else ''))

        if trigger_flag:
            print("[INFO] 수동 발행: ET/WIP는 현재 DB 사용, Inline은 기존 방식으로 조회합니다.")
            # 수신처: Scheduler.py가 환경변수 AUTO_REPORT_EMAIL_RECEIVER로 지정하면 그 그룹에만 발송된다
            #        (My_config.load_from_yaml에서 config.yaml의 email_receiver를 덮어씀)
            if os.getenv('AUTO_REPORT_EMAIL_RECEIVER'):
                print(f"[INFO] 트리거 수신 그룹 지정: {email_receiver}")
        print_status('이번 작업 대상', 'info', '아래 Lot ID와 DC Step별로 리포트를 처리합니다.')
        dc_done_list = pd.DataFrame(dc_done_list)
        _observe_measurements(final_lot_log,dc_done_list,GLOBAL_CONFIG)
        if not trigger_flag and report_making and not DB_Setting_mode:
            _queue_auto_reports(dc_done_list,vehicle)
        # A crash after this checkpoint must leave recoverable publication intent.
        atomic_output(Final_et_log_path, lambda temp: final_lot_log.to_csv(temp, index=False))
        if not trigger_flag and report_making and not DB_Setting_mode:
            dc_done_list=_resume_saved(dc_done_list,vehicle)

        if (not DB_Setting_mode) & (report_making):
            print_status('리포트 작성 시작', 'info',
                         '메일 발송 사용' if GLOBAL_CONFIG.get('use_email_send',False) else '파일 생성·저장만 수행 (메일 없음)')
            if not dc_done_list.empty:

                #dc_done_list
                dc_done_list['search_key'] = dc_done_list['lot_id'].astype(str) + '_' + dc_done_list['dc_step_id'].astype(str)
                search_strings = dc_done_list['search_key'].unique().tolist() #측정된 {fab_lot_id}_{dc_step_id} list

                # ================================================================
                # DuckDB: daily Hive 파티션 조회
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

                if trigger_mode == 'FORCE':
                    viewing_period = _force_viewing_period_all(
                        final_lot_log, trigger_lot, trigger_step, viewing_period, datetime_now)

                # DuckDB로 viewing_period 범위의 raw 데이터 로드
                _RUN.stage('raw_load')
                if trigger_mode == 'SINGLE':
                    # 쉼표 목록이면 여러 Lot/Step 을 한 번에(기간 제한 없이 대상 행만) 읽는다.
                    print(f'[INFO] SINGLE {vehicle}_{trigger_lot}_{trigger_step}: 기간 제한 없이 지정 lot/step만 조회')
                    raw_df = load_daily_projected(conn, DB_et_daily, None, reformatter,
                                                  lot=trigger_lot, step=trigger_step)
                else:
                    raw_df = load_daily_projected(conn, DB_et_daily, viewing_period, reformatter)

                if raw_df.empty:
                    scope = f'{trigger_lot}_{trigger_step} (기간 제한 없음)' if trigger_mode == 'SINGLE' else f'{viewing_period}일 이내'
                    raise ValueError(f'daily DB에 {scope} 필요한 측정 데이터 없음')

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
                if trigger_mode != 'SINGLE' and vehicle not in with_vehicle:
                    print('[INFO] 추가 비교 제품에 현재 제품은 없습니다. 선택된 비교 제품만 추가합니다.')
                    try : 
                        with_vehicle_Table = pd.DataFrame() 
                        for with_vehicle_now in with_vehicle :
                            wv_daily_path = DB + with_vehicle_now + '_daily'

                            print(f'[INFO] 비교 제품 {with_vehicle_now}: 최근 {viewing_period}일 측정 데이터 읽기')

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

                        target_lot_id = _lot
                        target_root_lot_id = target_lot_id[:5] #{root_lot_id}
                        target_DC_step_id = _step
                        target_DC_step = GLOBAL_CONFIG.get("dc_dict").get(target_DC_step_id) #{DC_step}
                        target_step_merged = (target_DC_step or target_DC_step_id) + "(" + target_DC_step_id + ")" #{DC_step_id}({DC_step})

                        match_key = target_root_lot_id + "_" + target_DC_step_id #match_key = {root_lot_id}_{DC_step_id}
                        # 리포트 키 = {fab_lot_id}_{step_id}(원본 키) — 통계 분석 근거/산출물
                        # rule_check/ARCHIVE 산출물 파일명이 전부 이 키를 공유(step별 덮어쓰기 방지)
                        report_key = f"{target_lot_id}_{target_DC_step_id}"
                        if trigger_mode == 'ALL':
                            _publish_all_trends(merged_df, reformatter, vehicle, target_lot_id,
                                                target_root_lot_id, target_DC_step, target_DC_step_id,
                                                trigger_mail, upload_date)
                            continue



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
                        print_status('Inline 측정 조회', 'info', f'Root Lot {target_root_lot_id}')
                        _RUN.stage('inline_query')
                        inlinedata = inlinedata_query(target_root_lot_id)
                        print_status('Inline 측정 조회', 'ok', f'Root Lot {target_root_lot_id}')

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
                        print_status('메일 첨부용 PPT 작성', 'info',
                                     f'제품 {vehicle} / Lot {target_lot_id} / DC Step {target_step_merged}')
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
                        #   순수 통계 판정(spec-out / Flier / 산포 / 수준 이동 / trend / SPC run)만 산출.
                        code_findings = []
                        anomaly_item_stats = {}   # 항목별 통계 요약
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
                                report_key=report_key)
                            print_status('측정 결과 통계 분석', 'ok', f'Spec 이탈·변화 등 검토 항목 {len(code_findings)}건')
                        except Exception as ce:
                            print(f"[WARN] 통계 분석을 완료하지 못했습니다. 리포트 내용 확인 필요: {ce}")
                        # 발행 스냅샷(RUN/ARCHIVE/<key>/) — 부가 산출물.
                        #   지워지거나 없어도 리포트 발행/판정에 영향 없음(저장 실패도 무시).
                        if getattr(GLOBAL_CONFIG, 'use_archive_snapshot', True):
                            try:
                                _save_archive_snapshot(
                                    report_key,
                                    {'report_key': report_key, 'target_lot_id': target_lot_id,
                                     'step_id': target_DC_step_id, 'dc_step': target_DC_step,
                                     'vehicle': vehicle, 'wafers': target_wafer_id_list,
                                     'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                                    code_findings, anomaly_item_stats,
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
                                item_slide_map=item_slide_map)
                        except Exception as fe:
                            print(f"[WARN] Anomaly 상세 페이지 삽입 스킵: {fe}")

                        # Score Board → 통계표(Index Aggregation Table) 순서로 인접 배치
                        _move_aggregation_after_scoreboard(prs_low_qual)

                        # 1-4. Save ppt - 메일링 버전
                        if not os.path.exists(low_qual_ppt_save_path):
                            os.makedirs(low_qual_ppt_save_path)
                        _RUN.stage('ppt_save')
                        # 메일 첨부 한도: 차트까지 만든 뒤 남은 용량으로 Description 이미지 화질을 정하고,
                        # 그래도 넘으면 큰 이미지부터 줄여 한도(ppt_mail_max_mb × ppt_budget_ratio) 아래로 맞춘다.
                        try:
                            _fit = fit_ppt_budget(prs_low_qual, GLOBAL_CONFIG)
                            print_status('PPT 용량 맞춤', 'ok' if _fit['after'] <= _fit['limit'] else 'fail',
                                         f"{_fit['before']/1e6:.2f}MB → {_fit['after']/1e6:.2f}MB (한도 {_fit['limit']/1e6:.1f}MB) · "
                                         f"설명 이미지 {_fit['desc_images']}장 {_fit['desc_level']}"
                                         + (f" · 생략 {_fit['desc_dropped']}" if _fit['desc_dropped'] else '')
                                         + (f" · 차트 축소 {_fit['charts_shrunk']}" if _fit['charts_shrunk'] else ''))
                        except Exception as _fit_err:
                            print(f"[WARN] PPT 용량 맞춤 생략: {_fit_err}")
                        atomic_output(f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}', prs_low_qual.save)
                        print_status('PPT 파일 저장', 'ok', '이어서 HTML 메일 본문을 작성합니다.')

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
                        print('[INFO] Pass Rate 표에 포함된 Lot:', ', '.join(map(str,_lots_sorted)))

                        # 측정값이 전혀 없는 행 제거 — PPT와 동일하게 lot-wafer reindex 후에도
                        # 첫 번째 dropna(VIP_group_HTML 초기 생성 시)를 통과한 항목은 유지.
                        # _existing_pr 필터(VIP_group_raw 생성 직후)로 미측정 항목은 이미 제거됨.
                        # 여기서 다시 dropna 하면 lot-wafer 분리 시 일부 lot에만 데이터가 있는
                        # 항목이 잘못 제거되어 "HTML에 2개만 표시"되는 버그 발생.
                        # NaN 셀은 HTML에서 빈 셀로 표시한다.

                        # ==================== Score Board HTML 렌더링 (Manual) ====================
                        # Pandas의 to_html()이 만드는 불안정한 멀티인덱스 태그를 방지하기 위해 HTML 태그를 한 땀 한 땀 생성
                        # - 좌측 고정열(LOT_ID/category/Item)은 클래스 기반 sticky (rowspan 사용해도 안깨짐)
                        # - category(CAT2) 연속 동일값은 rowspan으로 병합
                        score_board_html = _render_score_board(VIP_group_HTML, target_lot_id, display_name)

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

                        # ==================== [0] Anomaly: 코드 통계 분석 + Trend chart ====================
                        # analyze_commonality가 측정값으로 통계 검토 항목을 산출한다.
                        _top_n = getattr(GLOBAL_CONFIG, 'anomaly_trend_chart_top_n', 3)

                        # 1) 코드 통계 분석 결과(위 1-3b에서 계산) → HTML 요약
                        code_summary_html = ""
                        try:
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

                        def _is_excluded(_it):
                            return bool(item_excluded(_it, _excl_items))
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

                        # 일반 이상 항목(SPEC_OUT 등)으로 채움
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

                        # ==================== HTML 조립 ====================
                        sub_title = f'{target_lot_id} / {target_step_merged}'
                        html_content = html_code.replace('sub_title', sub_title)

                        # [0] 섹션 = 코드 자동 분석(통계 Finding) + Trend chart 그리드
                        # 섹션 제목/컨테이너 여백은 메일 클라이언트(<style> 무시)·포워딩에서도 동일하게
                        # 보이도록 inline style로 지정(class는 브라우저 sticky/스크롤 보조용으로 유지).
                        _SEC_T = ('border-left:4px solid #003366; padding-left:8px; font-size:15px; '
                                  'font-weight:bold; color:#003366; margin-top:20px; margin-bottom:6px;')
                        _TBL_C = 'margin-top:5px; margin-bottom:15px;'
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
                            f'{code_summary_html}{_chart_sub}{_chart_logic}{anomaly_html}</div>'
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
                        # 가독성: 메일 클라이언트는 <style> 을 무시하므로 inline 10px(표 셀) 글자를 11px 로 올린다.
                        # data URI(base64)에는 ':' 가 없어 이미지 내용과 겹치지 않는다.
                        html_content = html_content.replace('font-size:10px', 'font-size:11px')
                        html_content = _fit_html_budget(html_content)

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
                        _RUN.stage('score_save')
                        score_path = save_score_csv(VIP_group_HTML, DB, vehicle, target_lot_id,
                                                    target_DC_step_id, fname)
                        print_status('Score CSV 저장', 'ok', score_path)
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
                            # 전송은 네트워크 대기라 백그라운드 스레드로 보내고, 그동안 다음 Lot 차트를 그린다.
                            # 결과는 메인 스레드가 _drain_uploads()로 실행 이력에 반영한다.
                            _RUN.current["upload"]="sending"
                            _upload_async(client, _s3_local, bucket_dx, s3_key, _RUN.current, search_key)
                        else:
                            _RUN.current["upload"]="unavailable"
                            print_status("S3 업로드", "off", f"{search_key} → S3 미연결 스킵")

                        _RUN.stage('email')
                        _state = _send_report_files(_RUN.current['paths']['html'], _RUN.current['paths']['ppt'],
                                                  email_receiver, f'[HOL] {vehicle} {target_lot_id} {target_step_merged} HOL AUTO REPORT',
                                                  _RUN.current['id'])
                        _RUN.current['email']=_state
                        _RUN.save_report()
                        if _state not in ('sent','disabled'):
                            raise RuntimeError(f'메일 발송 {_state}: 운영 이력에서 수신처별 결과 확인 필요')

                        _completion = '리포트 발행 완료' if _state == 'sent' else '리포트 생성·저장 완료 (메일 없음)'
                        log_to_file(f"{search_key} {_completion}", query_log)
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
                        print_status(_completion, "ok",
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
                        _drain_uploads(block=False)   # 끝난 S3 전송 결과만 반영(기다리지 않음)
                        _release_memory()             # gc + (Linux) 빈 힙을 OS 에 반환 — 여러 Lot 연속 처리 시 RSS 누적 방지

                _drain_uploads(block=True)
            else:
                print_status('발행 대상 확인', 'skip', '현재 조건에 맞는 신규·재시도 Lot이 없습니다.')

        else:
            print_status('데이터 적재 완료', 'ok', '이번 작업은 DB 갱신만 수행하며 리포트를 발행하지 않습니다.')

        conn.close()

        shutdown_chart_pool()   # 병렬 렌더링 워커 풀 정리 (atexit에도 등록되어 있으나 명시 종료)
        print_status('제품 처리 종료', 'info', f'{vehicle} · 생성·저장·메일 결과를 최종 확인합니다.')

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
    stamp=re.sub(r'(\d{2}:\d{2}):\d{2}(?:\.\d+)?',r'\1',str(stamp))   # 초·마이크로초는 읽기만 어렵다
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+
            '</title></head><body style="margin:0;background:#ffffff">'
            '<div id="top" style="font-family:Segoe UI,Arial,Malgun Gothic,sans-serif;font-size:13px;'
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


def _assert_inline_images(html_content, expected=None):
    """HTML 인라인 이미지 불변식 검증(공통 헬퍼, 수정 시 주의).

    모든 <img> src는 data:image/...;base64 인라인이어야 한다.
    expected가 주어지면 이미지 개수까지 검증한다.
    위반 시 ValueError, 통과 시 이미지 개수를 반환한다.
    """
    sources=re.findall(r'<img\s[^>]*?src="([^"]*)"', html_content, re.DOTALL)
    bad=[s for s in sources if not s.startswith('data:image/')]
    if bad:
        raise ValueError('HTML 인라인 이미지 불변식 위반')
    if expected is not None and len(sources)!=expected:
        raise ValueError('HTML 인라인 이미지 불변식 위반')
    print(f"[INFO] HTML 인라인 이미지 검증 OK — <img> {len(sources)}개 모두 data:image 인라인")
    return len(sources)


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
    if 'auto_findings' in entry:
        findings=entry['auto_findings']
        if findings:
            critical=any(f['severity']=='CRITICAL' for f in findings)
            # 이상=빨강, 주의=주황 — 격자에서 색만 봐도 심각도가 갈린다.
            return ('이상' if critical else '주의'),('#b4232d' if critical else '#b45309'),'아래 Lot별 Auto Report 판정 근거를 확인하세요.'
        if entry.get('warnings'):return '자료 확인','#a45100','자료 제한을 확인한 뒤 추이를 해석하세요.'
        return '이상·주의 없음','#1f497d','최근 24시간 측정에서 Auto Report 기준 이상·주의 신호가 없습니다.'
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
        body+='<p>탐지 기준: 보정 q ≤ '+html.escape(str(settings.get('fdr_alpha',.05)))+' 및 각 기법의 효과 기준(순위 차이·상관·분포 거리·이상 비율 증가). Auto Report의 robust 산포 배수와 독립입니다. q는 불량률이 아니며 연관 후보는 원인 확정이 아닙니다.</p>'
        if settings.get('_influence_unavailable'):body+='<p style="color:#8a3800">연관 분석 불가: ML join 행 상한 초과로 시간·Split 정제 없이 탐지했습니다. 영향 후보는 참고하지 마세요.</p>'
        if analysis.get('budget_limited'):body+='<p style="color:#8a3800">연산 상한에 도달해 일부 검정을 생략했습니다. 탐지 없음은 전체 정상 판정이 아닙니다.</p>'
    else:
        body+='<p>검정 테두리 점은 발행 시점 직전 24시간 구간의 측정입니다. 과거 측정은 비교 배경이며 성공 발행 이력과 무관하게 동일한 24시간 구간을 강조합니다. Spec out은 표시 기간의 전체 유효 값 기준이며, 집계 항목은 설정된 집계값으로 계산합니다.</p>'
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
    # Daily Trend 본문은 html_columns(기본 3)열 격자 — 열이 많으면 작은 그림으로 그려 한 메일에 더 많은 항목을 싣는다.
    compact=settings.get('service')!='mlmode' and not settings.get('_ppt_chart') and int(settings.get('html_columns',3) or 3)>=3
    fig,ax=plt.subplots(figsize=(7.2,3.2) if settings.get('service')=='mlmode' else ((5.0,2.55) if compact else (7.2,2.65)))
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
            # 최근 24시간(하이라이트 구간)을 옅은 노란 띠로 — 검정 테두리 점과 함께 '오늘 무엇이 새로 찍혔나'를 한눈에.
            since,until=settings.get('highlight_since'),settings.get('report_now')
            if settings.get('service')!='mlmode' and since is not None and until is not None and not valid.empty:
                ax.axvspan(pd.Timestamp(since),pd.Timestamp(until),color='#ffe89c',alpha=.45,lw=0,zorder=0)
                ax.text(pd.Timestamp(since),1,' last 24h',transform=ax.get_xaxis_transform(),fontsize=8 if compact else 10,
                        va='top',ha='left',color='#8a5a00')
            locator=mdates.AutoDateLocator(minticks=3,maxticks=5 if compact else 6)
            ax.xaxis.set_major_locator(locator);ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
            if not valid.empty:
                daily=valid.groupby(valid['_time'].dt.floor('D'))['_value'].median().sort_index()
                median=daily.rolling('3D',min_periods=1).mean()
                ax.plot(median.index,median.values,color='black',lw=1.5,zorder=6)
                if not settings.get('_ppt_chart') and settings.get('service')!='mlmode':
                    from matplotlib.lines import Line2D
                    selected=legend_rows[:max(2,min(4 if compact else 99,int(settings.get('html_legend_limit',10))))]
                    handles=[Line2D([],[],marker='o',ls='',color=r['color'],markeredgecolor='black' if r['highlight'] else r['color'],markersize=5) for r in selected]
                    labels=[r['label'] if len(r['label'])<=32 else r['label'][:29]+'...' for r in selected]
                    handles.append(Line2D([],[],color='black',lw=1.5));labels.append('Daily median: 3D mean')
                    ax.legend(handles,labels,fontsize=8 if compact else 9,loc='upper left',ncol=3 if compact else 4,framealpha=.95,borderpad=.2,labelspacing=.15,columnspacing=.55,handletextpad=.25,handlelength=1.1,borderaxespad=.25,markerscale=.85)
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
        ax.set_xlabel(entry['x_label'],fontsize=11 if compact else 14,labelpad=2);ax.set_ylabel(entry['unit'],fontsize=11 if compact else 14,labelpad=2)
        for axis in (ax,):axis.tick_params(labelsize=10 if compact else 12,pad=2);axis.grid(alpha=.15)
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
    return ('색: 제품 / Split. 검정 테두리: 미발행 관측(첫 발행은 당일 측정). '
            '검정선: 전체 그룹의 일별 중앙값을 3일 이동평균한 참고선. X축: '+str(entry.get('x_label','시간 정보 없음'))+
            '. Y축: '+str(entry.get('unit','단위 정보 없음'))+' / '+str(entry['aggregation'])+'. 항목별 Y축 범위는 다를 수 있습니다.')


ML_MODULE_LABELS={'isolation_forest':'Isolation Forest 이상 증가','local_outlier_factor':'주변 패턴 대비 이상 증가',
                  'spatial_pattern':'웨이퍼 공간 패턴 변화','time_trend':'시간 추이 변화','split_difference':'Split 간 차이',
                  'equipment_difference':'장비 간 차이','spike_rate':'극단값 비율 증가',
                  'distribution_shift':'분포 변화','spread_change':'웨이퍼 산포 변화'}
# 기법별 '무엇을·왜 보는가' — 리포트 표에 그대로 나간다. My_config.mlmode['module_notes'] 로 문구를 바꿀 수 있다.
ML_MODULE_NOTES={
    'split_difference':'Split(knob) 그룹 간 Lot 요약값 순위 검정 — 공정 조건 차이로 값이 갈렸는지 확인',
    'time_trend':'Lot 순서에 따른 단조 증가·감소(순위 상관) — 서서히 움직이는 drift 확인',
    'distribution_shift':'과거 대비 신규 측정 분포 이동 — 수준(평균·중앙값) 변화 확인',
    'spread_change':'웨이퍼 내 산포 변화 — 균일도 악화 확인',
    'spike_rate':'robust σ 를 넘는 극단값 비율 증가 — 간헐적 불량 확인',
    'isolation_forest':'과거 wafer 로 학습한 Isolation Forest 가 신규 wafer 를 이상으로 보는 비율 — 여러 지표가 함께 틀어진 경우',
    'local_outlier_factor':'주변 밀도 대비 떨어진 신규 wafer 비율(LOF) — 국소적으로 튀는 wafer',
    'equipment_difference':'장비(eqp) 간 차이 — 특정 장비 영향 확인(진단용)',
    'spatial_pattern':'웨이퍼 위치별(센터·엣지) 패턴 변화 — 공간 불균일 확인(진단용)',
}


def _ml_candidate_source(settings):
    value=str(settings.get('candidate_source','either') or 'either').strip().lower()
    return value if value in ('daily','ml','either') else 'either'


def _ml_module_label(module):
    return ML_MODULE_LABELS.get(module,module)


def _ml_module_note(module, settings=None):
    custom=((settings or {}).get('module_notes') or {}) if isinstance(settings,dict) else {}
    return str(custom.get(module) or ML_MODULE_NOTES.get(module,''))


def _ml_finding_summary(entry):
    findings=entry.get('ml_findings',[])
    fallback=('ML 기법에서는 추가 신호 없음 — Daily Trend 판정(이상·주의)으로 포함된 항목' if entry.get('auto_findings')
              else entry.get('reason') or 'ML 신호 없음')
    summary=' · '.join(dict.fromkeys(_ml_module_label(f['module']) for f in findings)) or fallback
    if findings:summary+=' / 보정 q 최소 '+format(min(f['q'] for f in findings),'.3g')
    return summary


# ==================== ML mode 리포트 (실험 기능 · flow 디자인) ====================
# 한 항목 = auto report PPT 항목 페이지와 같은 구조(왼쪽 Box·WF MAP / 오른쪽 Trend·Radius·Cumulative)
# + ML_TABLE 인자 스크리닝 페이지(KNOB·MASK·EQP 범주 / INLINE·VM 수치 — R²·밑둥 들림).
# 메일 본문은 항목당 합성 이미지(시트 1장 + 인자 차트 1장)만 싣고, 메일 1통의 <img> 수를
# mail_inline_image_limit 이하로 나눠 보낸다(사내 메일 API 'Attach file count is over 10' 방지).
# 차트 안 글자는 ASCII 만 쓴다(사내 서버에 한글 글꼴이 없으면 두부 글자로 깨진다).
ML_UI=dict(ink='#171717',muted='#737373',line='#e5e5e5',subtle='#f5f5f5',page='#fafafa',panel='#ffffff',
           accent='#e25822',accent_bg='#fdf2eb',accent_line='#f5c2a8',info='#2563eb',info_bg='#eff6ff',
           ok='#16803c',ok_bg='#ecfdf3',warn='#b45309',warn_bg='#fffbeb',danger='#c81e1e',history='#7d93ab',new='#0f62fe')
ML_GROUP_COLORS=['#0f62fe','#e25822','#198038','#8a3ffc','#b28600','#007d79','#fa4d56','#6929c4','#1192e8','#9f1853','#005d5d','#570408']
_ML_FONT="'Segoe UI','Malgun Gothic',Arial,sans-serif"
# auto report 항목 페이지(My_Function.insert_plots)와 같은 좌/우 열 기하(inch)
_ML_LX,_ML_LW,_ML_RX,_ML_RW=0.12,8.30,8.50,4.70


def _ml_factor_text(row):
    """인자 한 줄 요약(한국어, 표·메일용)."""
    tq=int(round(100*row.get('tail_quantile',.1)))
    if row['kind']=='numeric':
        metric=f"R² {row.get('r2',0):.2f} · ρ {row.get('spearman',0):+.2f}"
    else:
        metric=f"ε² {row.get('epsilon2',0):.2f} · 수준 {row.get('levels',0)}"
    notes=[]
    for side in ('low_tail','high_tail'):
        if side in row['signals']:
            d=row[side]
            notes.append(('하단' if side=='low_tail' else '상단')+f" {tq}% wafer 비율: x 낮은 구간 {d['share_low_x']:.0%} → 높은 구간 {d['share_high_x']:.0%}")
    if 'level_tail' in row['signals']:
        d=row['level_tail'];notes.append(f"하단 {tq}% wafer 가 '{d['worst']}' 에 {d['worst_share']:.0%} 몰림")
    if 'level' in row['signals']:
        best=max(row.get('per_level',[]),key=lambda r:r['median'],default=None)
        worst=min(row.get('per_level',[]),key=lambda r:r['median'],default=None)
        if best and worst:notes.append(f"중앙값 '{best['x']}' {best['median']:.4g} vs '{worst['x']}' {worst['median']:.4g}")
    if 'r2' in row['signals']:notes.append(f"기울기 {row.get('slope',0):+.3g} / 단위 x")
    tests=row.get('tests',{})
    q=min([tests[s]['q'] for s in row['signals']] or [t['q'] for t in tests.values()] or [1.])
    verdict=' · '.join(_ml_signal_label(s) for s in row['signals']) or '신호 없음'
    return dict(metric=metric,note=' / '.join(notes),q=q,verdict=verdict)


def _ml_signal_label(signal):
    from My_Function import ML_FACTOR_SIGNALS
    return ML_FACTOR_SIGNALS.get(signal,signal)


def _mlv_axes(ax,title=None,xlabel=None,ylabel=None):
    ax.spines[['top','right']].set_visible(False)
    for side in ('left','bottom'):
        ax.spines[side].set_color('#525252');ax.spines[side].set_linewidth(.8)
    ax.tick_params(labelsize=7.5,color='#525252',width=.7,length=3,pad=2)
    ax.grid(alpha=.18,lw=.6)
    if title:ax.set_title(title,fontsize=9,loc='left',color=ML_UI['ink'],fontweight='bold',pad=4)
    if xlabel is not None:ax.set_xlabel(xlabel,fontsize=7.5,color='#404040',labelpad=2)
    if ylabel is not None:ax.set_ylabel(ylabel,fontsize=7.5,color='#404040',labelpad=2)


def _mlv_ascii(value, limit=40):
    text=re.sub(r'[^\x20-\x7E]','?',str(value))
    return text if len(text)<=limit else text[:limit-2]+'..'


def _mlv_save(fig, dpi):
    import io
    stream=io.BytesIO();fig.savefig(stream,format='png',dpi=dpi,facecolor='white');return stream.getvalue()


def _mlv_wafers(entry):
    """wafer 마다 최신 측정의 shot 중앙값 — Box·Cumulative 가 인자 스크리닝과 같은 단위를 쓴다."""
    raw=entry.get('spatial',pd.DataFrame())
    if raw is None or raw.empty:raw=entry.get('points',pd.DataFrame())
    if raw is None or raw.empty or '_value' not in raw:return pd.DataFrame()
    raw=raw.copy()
    if '_vehicle' in raw:raw=raw.loc[raw['_vehicle'].astype(str).eq(str(entry['vehicle']))]
    lot='root_lot_id' if 'root_lot_id' in raw else 'fab_lot_id'
    keys=[lot,'wafer_id']
    if any(k not in raw for k in keys) or raw.empty:return pd.DataFrame()
    if '_recent' not in raw:raw['_recent']=False
    clock='_dc_time' if '_dc_time' in raw else ('_time' if '_time' in raw else None)
    if clock:raw=raw.loc[raw[clock].eq(raw.groupby(keys)[clock].transform('max'))]
    extra=[c for c in raw if c.startswith('__ml_')]+(['_knob'] if '_knob' in raw else [])
    out=raw.groupby(keys,dropna=False).agg(y=('_value','median'),recent=('_recent','max'))
    if extra:out=out.join(raw.groupby(keys,dropna=False)[extra].first())
    return out.reset_index().rename(columns={lot:'lot'})


def _mlv_trend(entry, settings, size, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=size)
    try:
        points=entry.get('points',pd.DataFrame())
        valid=points.dropna(subset=['_time']) if not points.empty and '_time' in points else pd.DataFrame()
        if valid.empty:
            ax.text(.5,.5,'No timed measurements',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted'])
        else:
            rows=entry.get('_legend_rows') or _trend_legend_info(entry,settings)
            entry['_legend_rows']=rows
            colors={r['key']:r['color'] for r in rows}
            keys=['_vehicle','_knob'] if '_vehicle' in valid else '_knob'
            for key,group in valid.groupby(keys,sort=True):
                color=colors.get(key,ML_UI['new'])
                recent=group['_recent'].astype(bool) if '_recent' in group else pd.Series(False,index=group.index)
                ax.scatter(group.loc[~recent,'_time'],group.loc[~recent,'_value'],s=5,alpha=.35,color=color,edgecolors='none',rasterized=True)
                ax.scatter(group.loc[recent,'_time'],group.loc[recent,'_value'],s=11,color=color,edgecolors='black',linewidths=.45,zorder=4,rasterized=True)
            daily=valid.groupby(valid['_time'].dt.floor('D'))['_value'].median().sort_index().rolling('3D',min_periods=1).mean()
            ax.plot(daily.index,daily.values,color=ML_UI['ink'],lw=1.2,zorder=5)
            low,high=np.nanpercentile(valid['_value'],[.5,99.5])
            if np.isfinite(low) and np.isfinite(high) and high>low:
                pad=(high-low)*.08;ax.set_ylim(low-pad,high+pad)
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3,maxticks=5))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        _mlv_axes(ax,'Trend  (black edge = new)',None,_mlv_ascii(entry.get('unit','')))
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _mlv_group_column(entry, wafers):
    """Box 묶음 기준: 신호가 난 범주 인자 → Split(knob) → 과거/신규 순."""
    for row in entry.get('_factors',{}).get('flagged',[]):
        if row['kind']=='categorical' and '__ml_'+row['column'] in wafers:return '__ml_'+row['column'],row['column']
    if '_knob' in wafers and wafers['_knob'].nunique()>1:return '_knob','Split'
    return None,'History vs New'


def _mlv_box(entry, wafers, size, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=size)
    try:
        if wafers.empty:
            ax.text(.5,.5,'No wafer data',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted'])
            _mlv_axes(ax,'Box');fig.tight_layout(pad=.35);return _mlv_save(fig,dpi)
        column,label=_mlv_group_column(entry,wafers)
        data=wafers.dropna(subset=['y']).copy()
        data['g']=data[column].astype(str) if column else np.where(data['recent'].astype(bool),'New','History')
        groups=sorted(data['g'].unique())[:12] if column else [g for g in ('History','New') if g in set(data['g'])]
        for i,name in enumerate(groups):
            color=(ML_UI['history'] if name=='History' else ML_UI['new']) if not column else ML_GROUP_COLORS[i%len(ML_GROUP_COLORS)]
            values=data.loc[data['g'].eq(name)]
            box=ax.boxplot([values['y']],positions=[i],widths=.5,patch_artist=True,showfliers=False,
                           medianprops=dict(color=ML_UI['ink'],lw=1.2),whiskerprops=dict(color='#737373',lw=.8),capprops=dict(color='#737373',lw=.8))
            box['boxes'][0].set_facecolor(color);box['boxes'][0].set_alpha(.22);box['boxes'][0].set_edgecolor(color)
            jitter=((np.arange(len(values))*.6180339887)%1-.5)*.36
            recent=values['recent'].astype(bool).to_numpy()
            ax.scatter(i+jitter,values['y'],s=9,color=color,edgecolors=np.where(recent,'black','none'),linewidths=.5,zorder=3,alpha=.85)
            ax.hlines(values['y'].quantile(.1),i-.3,i+.3,colors=ML_UI['accent'],lw=1,linestyles=':',zorder=4)
        ax.set_xticks(range(len(groups)),[_mlv_ascii(g,22) for g in groups],rotation=0 if len(groups)<=6 else 25,ha='center' if len(groups)<=6 else 'right')
        _mlv_axes(ax,f'Box by {_mlv_ascii(label)}  (wafer median, dotted = P10)',None,_mlv_ascii(entry.get('unit','')))
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _mlv_geometry(entry):
    """제품 좌표 파일이 있으면 보정 좌표(ADJ)로, 없으면 측정 격자로 — _ml_spatial_details 와 같은 규칙."""
    import My_Function as wf
    raw=entry.get('spatial',pd.DataFrame())
    need={'chip_x_pos','chip_y_pos','_value','_recent'}
    if raw is None or raw.empty or not need.issubset(raw):return None
    group=raw.copy()
    if '_vehicle' in group:group=group.loc[group['_vehicle'].astype(str).eq(str(entry['vehicle']))]
    if 'flat_zone' in group and group['flat_zone'].nunique()>1:
        group=group.loc[group['flat_zone'].eq(group['flat_zone'].mode().iloc[0])]
    for c in ('chip_x_pos','chip_y_pos'):group[c]=pd.to_numeric(group[c],errors='coerce')
    group=group.dropna(subset=['chip_x_pos','chip_y_pos','_value'])
    if group.empty:return None
    vehicle=entry['vehicle'];calibrated=False
    layout=getattr(wf,'_CHIP_LAYOUT',None)
    if layout is not None and {'MASK','CHIP_X_POS','CHIP_Y_POS','CHIP_X_ADJ','CHIP_Y_ADJ'}.issubset(layout):
        chosen=layout.loc[layout.MASK.astype(str).eq(str(vehicle))].copy()
        join={'CHIP_X_POS':'chip_x_pos','CHIP_Y_POS':'chip_y_pos'}
        if 'flat_zone' in group and 'FLAT_ZONE_POS' in chosen:join['FLAT_ZONE_POS']='flat_zone'
        cols=list(join)+['CHIP_X_ADJ','CHIP_Y_ADJ']+(['Chip_Radius'] if 'Chip_Radius' in chosen and 'Chip_Radius' not in group else [])
        chosen=chosen[cols].rename(columns=join).drop_duplicates()
        keys=list(join.values())
        for key in keys:   # 좌표 파일은 숫자, DB 는 문자열일 수 있다 — 같은 형으로 맞춘 뒤 붙인다.
            left=pd.to_numeric(group[key],errors='coerce');right=pd.to_numeric(chosen[key],errors='coerce')
            if left.notna().all() and right.notna().all():group[key]=left;chosen[key]=right
            else:group[key]=group[key].astype(str);chosen[key]=chosen[key].astype(str)
        if len(chosen) and not chosen.duplicated(keys).any():
            merged=group.merge(chosen,on=keys,how='left',validate='many_to_one')
            merged=merged.dropna(subset=['CHIP_X_ADJ','CHIP_Y_ADJ'])
            if not merged.empty:group=merged;calibrated=True
    cx,cy=('CHIP_X_ADJ','CHIP_Y_ADJ') if calibrated else ('chip_x_pos','chip_y_pos')
    circ=wf._wafer_circle_params(group,cx,cy,'Chip_Radius' if 'Chip_Radius' in group else None,main_vehicle=vehicle)
    pitch=wf._wfmap_shot_pitch_xy(group,cx,cy,main_vehicle=vehicle)
    limits=wf._wfmap_axis_limits(*wf._wfmap_grid_limits(group,cx,cy,main_vehicle=vehicle),circ)
    lot='root_lot_id' if 'root_lot_id' in group else 'fab_lot_id'
    return dict(frame=group,cx=cx,cy=cy,circ=circ,pitch=pitch,limits=limits,lot=lot,calibrated=calibrated)


def _mlv_maps(entry, geometry, size, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    import My_Function as wf
    plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(1,2,figsize=size)
    try:
        if geometry is None:
            for ax,title in zip(axes,('New - site median','New minus History')):
                ax.text(.5,.5,'No shot X/Y',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted']);ax.set_axis_off()
                ax.set_title(title,fontsize=9,loc='left',fontweight='bold')
            fig.tight_layout(pad=.35);return _mlv_save(fig,dpi)
        g=geometry['frame'];cx,cy,lot=geometry['cx'],geometry['cy'],geometry['lot']
        wafer=g.groupby([lot,'wafer_id','_recent',cx,cy])['_value'].median().reset_index()
        roots=wafer.groupby([lot,'_recent',cx,cy])['_value'].median().reset_index()
        site=roots.groupby(['_recent',cx,cy])['_value'].median().unstack('_recent')
        new=site[True].dropna() if True in site else pd.Series(dtype=float)
        delta=(site[True]-site[False]).dropna() if True in site and False in site else pd.Series(dtype=float)
        panels=[(axes[0],new,'viridis','New - site median'),(axes[1],delta,'coolwarm','New minus History')]
        for ax,series,cmap,title in panels:
            ax.set_title(title,fontsize=9,loc='left',fontweight='bold',color=ML_UI['ink'],pad=3)
            if series.empty:
                ax.text(.5,.5,'No matched sites',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted'])
            else:
                xs=series.index.get_level_values(0);ys=series.index.get_level_values(1)
                if cmap=='coolwarm':
                    span=max(float(series.abs().max()),1e-12);norm=Normalize(-span,span)
                else:norm=Normalize(float(series.min()),float(series.max()) if series.max()>series.min() else float(series.min())+1e-12)
                shots=wf._draw_wfmap_shots(ax,xs,ys,*geometry['pitch'],values=series.values,cmap=cmap,norm=norm)
                bar=fig.colorbar(shots,ax=ax,pad=.02,fraction=.05);bar.ax.tick_params(labelsize=7)
            wf._add_wafer_circle(ax,geometry['circ'],color='#262626',lw=1.1,zorder=4)
            limits=geometry['limits']
            ax.set_xlim(limits[0],limits[1]);ax.set_ylim(limits[3],limits[2])
            ax.set_aspect(wf._wfmap_aspect(geometry['circ']),adjustable='box')
            ax.set_xticks([]);ax.set_yticks([])
            for spine in ax.spines.values():spine.set_visible(False)
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _mlv_radius(entry, geometry, size, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=size)
    try:
        if geometry is None:
            ax.text(.5,.5,'No shot X/Y',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted'])
        else:
            g=geometry['frame'].copy();circ=geometry['circ'];lot=geometry['lot']
            if 'Chip_Radius' in g and g['Chip_Radius'].notna().any():
                g['radius']=pd.to_numeric(g['Chip_Radius'],errors='coerce');xlabel='Radius (mm)'
            else:
                g['radius']=np.hypot(g[geometry['cx']]-(circ[0] if circ else 0),g[geometry['cy']]-(circ[1] if circ else 0));xlabel='Radius (grid)'
            g=g.dropna(subset=['radius'])
            g['bin']=pd.cut(g['radius'],bins=min(10,max(2,g['radius'].nunique())),labels=False)
            for recent,label,color in ((False,'History',ML_UI['history']),(True,'New',ML_UI['new'])):
                part=g.loc[g['_recent'].astype(bool).eq(recent)]
                if part.empty:continue
                per=part.groupby([lot,'bin']).agg(r=('radius','median'),v=('_value','median')).reset_index()
                profile=per.groupby('bin').agg(r=('r','median'),v=('v','median')).sort_values('r')
                ax.plot(profile['r'],profile['v'],'o-',ms=3,lw=1.3,color=color,label=label)
            ax.legend(fontsize=7,frameon=False,loc='best')
            ax.set_xlabel(xlabel,fontsize=7.5)
        _mlv_axes(ax,'Radius  (root-lot balanced median)',None,_mlv_ascii(entry.get('unit','')))
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _mlv_cdf(entry, wafers, size, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=size)
    try:
        if wafers.empty:
            ax.text(.5,.5,'No wafer data',ha='center',va='center',transform=ax.transAxes,color=ML_UI['muted'])
        else:
            for recent,label,color in ((False,'History',ML_UI['history']),(True,'New',ML_UI['new'])):
                values=np.sort(wafers.loc[wafers['recent'].astype(bool).eq(recent),'y'].dropna().to_numpy(float))
                if not len(values):continue
                ax.step(values,np.arange(1,len(values)+1)/len(values)*100,where='post',color=color,lw=1.4,label=f'{label} (n={len(values)})')
            ax.axhline(10,color=ML_UI['accent'],lw=.8,ls=':')
            ax.legend(fontsize=7,frameon=False,loc='lower right')
            ax.set_ylim(0,100)
        _mlv_axes(ax,'Cumulative  (wafer median, dotted = 10%)',_mlv_ascii(entry.get('unit','')),'%')
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _mlv_factor(entry, row, size, dpi):
    """인자 1개 차트 — 수치: 산점 + x 구간별 P10/P50/P90(밑둥), 범주: 수준별 Box + P10."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=size)
    try:
        plot=row.get('plot',{})
        x=plot.get('x',[]);y=np.asarray(plot.get('y',[]),dtype=float);recent=np.asarray(plot.get('recent',[]),dtype=bool)
        tq=int(round(100*row.get('tail_quantile',.1)))
        if row['kind']=='numeric':
            x=np.asarray(x,dtype=float)
            ax.scatter(x[~recent],y[~recent],s=10,color=ML_UI['history'],alpha=.6,edgecolors='none',label='History')
            ax.scatter(x[recent],y[recent],s=14,color=ML_UI['new'],edgecolors='black',linewidths=.4,label='New',zorder=3)
            profile=pd.DataFrame(row.get('profile',[]))
            if not profile.empty:
                for key,side in (('p10','low_tail'),('p50',None),('p90','high_tail')):
                    hot=side in row['signals'] if side else False
                    ax.plot(profile['x'],profile[key],'-o' if hot else '--',ms=3,lw=1.8 if hot else .9,
                            color=ML_UI['accent'] if hot else ('#404040' if key=='p50' else '#a3a3a3'),zorder=4,
                            label=(f'P{tq}' if key=='p10' else ('P50' if key=='p50' else f'P{100-tq}'))+(' (tail shift)' if hot else ''))
            if 'r2' in row['signals'] and len(x)>2:
                fit=np.polyfit(x,y,1);grid=np.linspace(np.nanmin(x),np.nanmax(x),50)
                ax.plot(grid,np.polyval(fit,grid),color=ML_UI['ink'],lw=1.4,zorder=5,label='Linear fit')
            head=f"R2={row.get('r2',0):.2f}  rho={row.get('spearman',0):+.2f}  n={row.get('wafers',0)} wafers / {row.get('lots',0)} lots"
            _mlv_axes(ax,f"{row['family']} | {_mlv_ascii(row['column'],44)}",_mlv_ascii(row['column'],44),_mlv_ascii(entry.get('item',''),30))
        else:
            levels=sorted(set(map(str,x)))
            for i,level in enumerate(levels):
                mask=np.asarray([str(v)==level for v in x])
                color=ML_GROUP_COLORS[i%len(ML_GROUP_COLORS)]
                box=ax.boxplot([y[mask]],positions=[i],widths=.5,patch_artist=True,showfliers=False,
                               medianprops=dict(color=ML_UI['ink'],lw=1.2),whiskerprops=dict(color='#737373'),capprops=dict(color='#737373'))
                box['boxes'][0].set_facecolor(color);box['boxes'][0].set_alpha(.22);box['boxes'][0].set_edgecolor(color)
                jitter=((np.arange(mask.sum())*.6180339887)%1-.5)*.36
                ax.scatter(i+jitter,y[mask],s=10,color=color,edgecolors=np.where(recent[mask],'black','none'),linewidths=.45,alpha=.85,zorder=3)
                hot='level_tail' in row['signals']
                ax.hlines(np.quantile(y[mask],row.get('tail_quantile',.1)),i-.32,i+.32,colors=ML_UI['accent'] if hot else '#a3a3a3',
                          lw=1.6 if hot else .9,linestyles='-' if hot else ':',zorder=4)
            ax.set_xticks(range(len(levels)),[_mlv_ascii(v,18) for v in levels],rotation=0 if len(levels)<=5 else 25,ha='center' if len(levels)<=5 else 'right')
            head=f"eps2={row.get('epsilon2',0):.2f}  {row.get('levels',0)} levels  n={row.get('wafers',0)} wafers / {row.get('lots',0)} lots"
            _mlv_axes(ax,f"{row['family']} | {_mlv_ascii(row['column'],44)}",None,_mlv_ascii(entry.get('item',''),30))
        ax.text(.01,.98,head,transform=ax.transAxes,fontsize=7,va='top',color='#404040',
                bbox=dict(boxstyle='square,pad=.25',fc='white',ec='none',alpha=.85),zorder=6)
        if row['kind']=='numeric':ax.legend(fontsize=6.5,frameon=False,loc='lower right',ncol=3)
        fig.tight_layout(pad=.35)
        return _mlv_save(fig,dpi)
    finally:plt.close(fig)


def _ml_item_render(entry, settings):
    """한 항목의 차트 묶음(PPT 용 개별 그림 + 메일 용 합성 그림)."""
    dpi=int(settings.get('chart_dpi',150))
    wafers=_mlv_wafers(entry);geometry=_mlv_geometry(entry)
    panels=dict(trend=_mlv_trend(entry,settings,(_ML_RW,1.95),dpi),
                box=_mlv_box(entry,wafers,(_ML_LW,1.95),dpi),
                map=_mlv_maps(entry,geometry,(_ML_LW-.2,2.3),dpi),
                radius=_mlv_radius(entry,geometry,(_ML_RW,1.95),dpi),
                cdf=_mlv_cdf(entry,wafers,(_ML_RW,2.2),dpi))
    factors=[_mlv_factor(entry,row,(6.3,2.55),dpi) for row in entry.get('_factors',{}).get('flagged',[])]
    return dict(panels=panels,factors=factors,sheet=_ml_compose_sheet(panels,dpi),
                factor_sheet=_ml_compose_factors(factors) if factors else None,factor_columns=2 if len(factors)>1 else 1)


def _ml_png_palette(image, colors=256):
    """합성 그림을 팔레트 PNG 로 — 차트는 색 수가 적어 무손실에 가깝게 작아진다.
    octree 는 적은 면적의 색(컬러바 끝·강조선)도 살린다(median-cut 은 viridis 노랑을 주황으로 뭉갰다)."""
    import io
    from PIL import Image
    packed=image.convert('RGB').quantize(colors=colors,method=Image.Quantize.FASTOCTREE,dither=Image.Dither.NONE)
    stream=io.BytesIO();packed.save(stream,format='PNG',optimize=True);return stream.getvalue()


def _ml_compose_sheet(panels, dpi, width=1320):
    """auto report 항목 페이지와 같은 배치로 5개 차트를 한 장에(메일 이미지 수 절약)."""
    import io
    from PIL import Image
    load=lambda key:Image.open(io.BytesIO(panels[key])).convert('RGB')
    px=lambda inch:int(round(inch*dpi))
    gap=px(.08)
    left=[load('box'),load('map')];right=[load('trend'),load('radius'),load('cdf')]
    lw=max(im.width for im in left);rw=max(im.width for im in right)
    height=max(sum(im.height for im in left)+gap,sum(im.height for im in right)+2*gap)
    canvas=Image.new('RGB',(lw+gap+rw,height),'white')
    y=0
    for im in left:canvas.paste(im,(0,y));y+=im.height+gap
    y=0
    for im in right:canvas.paste(im,(lw+gap,y));y+=im.height+gap
    if canvas.width>width:canvas=canvas.resize((width,int(canvas.height*width/canvas.width)),Image.Resampling.LANCZOS)
    return _ml_png_palette(canvas)


def _ml_compose_factors(images, width=1320):
    import io
    from PIL import Image
    tiles=[Image.open(io.BytesIO(b)).convert('RGB') for b in images]
    cols=2 if len(tiles)>1 else 1
    tw=max(t.width for t in tiles);th=max(t.height for t in tiles)
    rows=(len(tiles)+cols-1)//cols
    canvas=Image.new('RGB',(tw*cols,th*rows),'white')
    for i,tile in enumerate(tiles):canvas.paste(tile,((i%cols)*tw,(i//cols)*th))
    target=width if cols==2 else width//2
    if canvas.width>target:canvas=canvas.resize((target,int(canvas.height*target/canvas.width)),Image.Resampling.LANCZOS)
    return _ml_png_palette(canvas)


def _ml_item_state(entry):
    """배지 문구·색 — ML 검정 신호 > 인자 신호 > Daily 판정 순."""
    if entry.get('ml_findings'):return 'ML 신호',ML_UI['accent'],ML_UI['accent_bg']
    if entry.get('_factors',{}).get('flagged'):return '인자 연관',ML_UI['info'],ML_UI['info_bg']
    if entry.get('auto_findings'):return 'Daily 판정',ML_UI['warn'],ML_UI['warn_bg']
    return '참고',ML_UI['muted'],ML_UI['subtle']


def _ml_item_priority(entry):
    findings=entry.get('ml_findings',[])
    flagged=entry.get('_factors',{}).get('flagged',[])
    return (0 if findings else 1,0 if flagged else 1,min([f['q'] for f in findings] or [1.]),-len(flagged),
            entry['vehicle'],entry['category'],entry['item'],entry['step'])


def _ml_chip(text, color, background):
    import html
    return ('<span style="display:inline-block;margin:0 6px 4px 0;padding:2px 8px;border:1px solid '+color+';color:'+color+
            ';background:'+background+';font-size:12px;line-height:18px;border-radius:4px;white-space:nowrap">'+html.escape(str(text))+'</span>')


def _ml_table_html(headers, rows, highlight=None, widths=None):
    """flow 톤 표 — 헤더 옅은 회색, 줄 구분선만. highlight(i) 가 참이면 왼쪽 주황 막대."""
    import html
    esc=lambda v:html.escape(str(v))
    out=('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12px;'
         'line-height:1.5;font-family:'+_ML_FONT+';color:'+ML_UI['ink']+'"><tr>')
    for i,h in enumerate(headers):
        w=(' width="'+str(widths[i])+'"') if widths else ''
        out+='<th'+w+' align="left" style="padding:6px 8px;background:'+ML_UI['subtle']+';color:'+ML_UI['muted']+';font-weight:600;border-bottom:1px solid '+ML_UI['line']+'">'+esc(h)+'</th>'
    out+='</tr>'
    for i,row in enumerate(rows):
        hot=bool(highlight and highlight(i))
        out+='<tr>'
        for j,cell in enumerate(row):
            edge='border-left:3px solid '+ML_UI['accent']+';' if hot and j==0 else ('border-left:3px solid transparent;' if j==0 else '')
            raw=isinstance(cell,tuple)
            out+=('<td style="'+edge+'padding:6px 8px;vertical-align:top;border-bottom:1px solid '+ML_UI['line']+';overflow-wrap:anywhere">'
                  +(cell[0] if raw else esc(cell))+'</td>')
        out+='</tr>'
    if not rows:out+='<tr><td colspan="'+str(len(headers))+'" style="padding:10px 8px;color:'+ML_UI['muted']+'">해당 없음</td></tr>'
    return out+'</table>'


def _ml_factor_rows(entry, limit=12):
    rows=[]
    for row in entry.get('_factors',{}).get('rows',[])[:limit]:
        text=_ml_factor_text(row)
        rows.append([row['family'],row['column'],'수치' if row['kind']=='numeric' else '범주',text['metric'],
                     format(text['q'],'.2g'),text['verdict']+((' — '+text['note']) if text['note'] else '')])
    return rows


def _ml_item_card(entry, render, settings, index, total):
    import html
    esc=lambda v:html.escape(str(v))
    state,color,background=_ml_item_state(entry)
    anchor=_trend_anchor(entry)
    meta=' · '.join(str(v) for v in (entry['vehicle'],entry['category'],entry['step'],entry['program'],entry['temperature'],entry.get('unit','')) if str(v))
    card=('<table role="presentation" id="'+anchor+'" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px;background:'+ML_UI['panel']+
          ';border:1px solid '+ML_UI['line']+';border-radius:8px;border-collapse:separate"><tr><td style="padding:14px 16px 10px">'
          '<a name="'+anchor+'"></a>'
          '<div style="font-size:11px;color:'+ML_UI['muted']+';letter-spacing:.02em">ITEM '+str(index)+' / '+str(total)+'</div>'
          '<div style="margin:2px 0 4px"><span style="font-size:18px;font-weight:700;color:'+ML_UI['ink']+'">'+esc(entry['item'])+'</span>'
          '&nbsp;&nbsp;'+_ml_chip(state,color,background)+'</div>'
          '<div style="font-size:12px;color:'+ML_UI['muted']+'">'+esc(meta)+' &nbsp;|&nbsp; N='+f"{entry.get('n',0):,}"+' · lot '+str(entry.get('lots',0))+' · 신규 lot '+str(entry.get('recent_lots',0))+'</div>')
    chips=''
    for f in entry.get('auto_findings',[])[:3]:chips+=_ml_chip('Daily · '+str(f['title'])[:40],ML_UI['warn'],ML_UI['warn_bg'])
    for module in dict.fromkeys(f['module'] for f in entry.get('ml_findings',[])):chips+=_ml_chip('ML · '+_ml_module_label(module),ML_UI['accent'],ML_UI['accent_bg'])
    for row in entry.get('_factors',{}).get('flagged',[]):
        chips+=_ml_chip(row['column']+' · '+' / '.join(_ml_signal_label(s) for s in row['signals']),ML_UI['info'],ML_UI['info_bg'])
    if chips:card+='<div style="margin-top:8px">'+chips+'</div>'
    card+='</td></tr>'
    card+=('<tr><td style="padding:0 8px"><img alt="'+esc(entry['item'])+' 차트" width="1000" style="display:block;width:100%;max-width:1320px;height:auto;margin:0 auto" src="'
           +_img_datauri(render['sheet'])+'"></td></tr>')
    card+=('<tr><td style="padding:4px 16px 0;font-size:11px;color:'+ML_UI['muted']+'">왼쪽: Box(인자·Split별 wafer 중앙값) · WF MAP(신규 / 신규−과거) &nbsp;|&nbsp; '
           '오른쪽: Trend · Radius · Cumulative — auto report 항목 페이지와 같은 배치</td></tr>')
    factors=entry.get('_factors',{})
    card+='<tr><td style="padding:14px 16px 4px"><div style="font-size:14px;font-weight:700;margin-bottom:6px">ML_TABLE 인자 스크리닝</div>'
    fam=factors.get('families',{})
    if fam:
        card+='<div style="margin-bottom:6px">'+''.join(_ml_chip(f"{k} {v['flagged']}/{v['tested']}",ML_UI['info'] if v['flagged'] else ML_UI['muted'],ML_UI['info_bg'] if v['flagged'] else ML_UI['subtle']) for k,v in sorted(fam.items()))+'</div>'
    rows=_ml_factor_rows(entry)
    card+=_ml_table_html(['계열','인자','종류','지표','q','판정 / 근거'],rows,highlight=lambda i:bool(entry['_factors']['rows'][i]['signals']),
                         widths=[60,170,44,150,50,None])
    if factors.get('skipped'):
        card+='<div style="font-size:11px;color:'+ML_UI['muted']+';margin-top:4px">검사 제외: '+esc(' / '.join(factors['skipped'][:6]))+'</div>'
    card+='</td></tr>'
    if render.get('factor_sheet'):
        wide=render.get('factor_columns',2)>1   # 인자 1개면 반폭으로(원본 해상도 이상으로 키우지 않는다)
        card+=('<tr><td style="padding:6px 8px 0"><img alt="'+esc(entry['item'])+' 인자 차트" width="'+('1000' if wide else '500')+'" style="display:block;width:'+('100%' if wide else '50%')+';max-width:'+('1320' if wide else '660')+'px;height:auto;margin:0" src="'
               +_img_datauri(render['factor_sheet'])+'"></td></tr>')
        card+=('<tr><td style="padding:2px 16px 0;font-size:11px;color:'+ML_UI['muted']+'">수치 인자: 점 = wafer, 선 = x 구간별 P10 / P50 / P90 — 주황 선이 한쪽 꼬리만 움직이면 “밑둥 들림”. '
               '범주 인자: 수준별 Box, 주황 가로선 = 수준별 P10.</td></tr>')
    findings=entry.get('ml_findings',[])
    card+='<tr><td style="padding:14px 16px 4px"><div style="font-size:14px;font-weight:700;margin-bottom:6px">탐지 근거</div>'
    card+=_ml_table_html(['기법','무엇을 보는가','비교 / 근거','보정 q','효과 / 기준'],
        [[_ml_module_label(f['module']),_ml_module_note(f['module'],settings),f['message'],f"{f['q']:.3g}",f"{f['effect']:.3g} / {f.get('minimum_effect','-')}"] for f in findings[:8]]
        +[['Daily 판정',str(f.get('lot','')),str(f['title']),'-','-'] for f in entry.get('auto_findings',[])[:4]],widths=[130,220,None,60,90])
    restrictions=list(dict.fromkeys(entry.get('warnings',[])))
    if restrictions:card+='<div style="font-size:11px;color:'+ML_UI['warn']+';margin-top:6px">자료 제한: '+esc(' / '.join(restrictions[:6]))+'</div>'
    card+='</td></tr><tr><td style="padding:8px 16px 12px;text-align:right;font-size:12px"><a href="#top" style="color:'+ML_UI['accent']+';text-decoration:none">목록으로 ↑</a></td></tr></table>'
    return card


def _ml_html(parts_entries, renders, settings, title, part_no, part_count, all_entries):
    import html
    esc=lambda v:html.escape(str(v))
    analysis=settings.get('_analysis',{})
    stamp=re.sub(r'(\d{2}:\d{2}):\d{2}(?:\.\d+)?',r'\1',str(settings.get('report_now','')))
    body=('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title></head>'
          '<body style="margin:0;background:'+ML_UI['page']+'"><div id="top" style="font-family:'+_ML_FONT+';font-size:13px;color:'+ML_UI['ink']+
          ';line-height:1.5;padding:20px 24px;background:'+ML_UI['page']+';max-width:1400px;margin:0 auto">')
    body+=('<div style="font-size:11px;font-weight:700;color:'+ML_UI['accent']+';letter-spacing:.06em">AUTO REPORT · ML MODE <span style="font-weight:400;color:'+ML_UI['muted']+'">· 실험 기능</span></div>'
           '<h1 style="margin:4px 0 2px;font-size:22px;font-weight:700;color:'+ML_UI['ink']+'">'+esc(title)+'</h1>'
           '<div style="color:'+ML_UI['muted']+';font-size:12px">분석 시각 '+esc(stamp)+(' · 메일 '+str(part_no)+' / '+str(part_count) if part_count>1 else '')+'</div>'
           '<div style="height:3px;background:'+ML_UI['accent']+';margin:12px 0 16px;width:64px"></div>')
    flagged=sum(bool(e.get('ml_findings')) for e in all_entries)
    related=sum(bool(e.get('_factors',{}).get('flagged')) for e in all_entries)
    families={}
    for e in all_entries:
        for k,v in e.get('_factors',{}).get('families',{}).items():
            f=families.setdefault(k,dict(tested=0,flagged=0));f['tested']+=v['tested'];f['flagged']+=v['flagged']
    tiles=[('검토 항목',len(all_entries),ML_UI['ink']),('ML 신호',flagged,ML_UI['accent']),('인자 연관',related,ML_UI['info']),
           ('통계 검정',analysis.get('statistical_tests',0),ML_UI['muted'])]
    body+='<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:separate;border-spacing:8px 0;margin:0 -8px 14px"><tr>'
    body+=''.join('<td style="background:'+ML_UI['panel']+';border:1px solid '+ML_UI['line']+';border-radius:8px;padding:10px 14px"><div style="font-size:12px;color:'+ML_UI['muted']+'">'+esc(k)+
                  '</div><div style="font-size:24px;font-weight:700;color:'+c+'">'+esc(v)+'</div></td>' for k,v,c in tiles)+'</tr></table>'
    if families:
        body+=('<div style="margin:0 0 12px"><span style="font-size:12px;color:'+ML_UI['muted']+';margin-right:8px">인자 계열 (신호 / 검사)</span>'
               +''.join(_ml_chip(f"{k} {v['flagged']}/{v['tested']}",ML_UI['info'] if v['flagged'] else ML_UI['muted'],ML_UI['info_bg'] if v['flagged'] else ML_UI['panel']) for k,v in sorted(families.items()))+'</div>')
    rows=[]
    for i,e in enumerate(parts_entries,1):
        state,color,background=_ml_item_state(e)
        factor=' / '.join(r['column']+' · '+' '.join(_ml_signal_label(s) for s in r['signals']) for r in e.get('_factors',{}).get('flagged',[])[:2])
        rows.append([(_ml_chip(state,color,background),),('<a href="#'+_trend_anchor(e)+'" style="color:'+ML_UI['ink']+';font-weight:700;text-decoration:none">'+esc(e['item'])+'</a>'
                     '<div style="font-size:11px;color:'+ML_UI['muted']+'">'+esc(' · '.join(str(v) for v in (e['vehicle'],e['category'],e['step'],e['program'],e['temperature'])))+'</div>',),
                     e.get('selection_reason') or _ml_finding_summary(e),factor or '-',f"{e.get('recent_lots',0)} / {e.get('lots',0)}"])
    body+='<div style="background:'+ML_UI['panel']+';border:1px solid '+ML_UI['line']+';border-radius:8px;padding:12px 14px;margin-bottom:20px">'
    body+='<div style="font-size:14px;font-weight:700;margin-bottom:6px">이 메일의 항목 '+str(len(parts_entries))+'개'+(f' <span style="font-weight:400;color:{ML_UI["muted"]};font-size:12px">(전체 {len(all_entries)}개 중)</span>' if len(all_entries)>len(parts_entries) else '')+'</div>'
    body+=_ml_table_html(['상태','항목','선정 이유','인자 신호','신규 / 전체 lot'],rows,widths=[90,220,None,220,90])+'</div>'
    for i,e in enumerate(parts_entries,1):body+=_ml_item_card(e,renders[id(e)],settings,i,len(parts_entries))
    notes=['q 는 BH 보정 p 값이며 불량률이 아닙니다. 연관 신호는 원인 확정이 아닌 탐색 결과입니다.',
           '인자 스크리닝 단위는 wafer(최신 측정 shot 중앙값)입니다. 같은 lot wafer 는 독립이 아니므로 유효 표본을 줄여 검정합니다.',
           '“밑둥 들림” = x 가 변할 때 분포의 한쪽 꼬리(P'+str(int(round(100*float(settings.get('factor_tail_quantile',.1)))))+')만 움직이고 반대쪽은 그대로인 경우입니다.']
    if settings.get('_influence_unavailable'):notes.append('ML join 행 상한 초과로 인자 연관 분석을 하지 못했습니다.')
    if analysis.get('budget_limited'):notes.append('연산 상한에 도달해 일부 검정을 생략했습니다. 탐지 없음은 전체 정상 판정이 아닙니다.')
    body+='<div style="font-size:11px;color:'+ML_UI['muted']+';border-top:1px solid '+ML_UI['line']+';padding-top:8px">'+'<br>'.join(esc(n) for n in notes)+'</div>'
    return body+'</div></body></html>'


def _ml_ppt(parts_entries, renders, settings, title):
    import io
    from pptx import Presentation
    from pptx.util import Inches,Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN,MSO_ANCHOR
    from My_Function import _add_internal_slide_link
    rgb=lambda h:RGBColor.from_string(h.lstrip('#'))
    font=getattr(GLOBAL_CONFIG,'theme_font_family','Malgun Gothic')
    prs=Presentation();prs.slide_width=Inches(13.333);prs.slide_height=Inches(7.5)

    def text(slide,value,x,y,w,h,size=11,bold=False,color=ML_UI['ink'],align=None):
        frame=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h)).text_frame
        frame.word_wrap=True;frame.margin_left=frame.margin_right=frame.margin_top=frame.margin_bottom=0
        for i,line in enumerate(str(value).split('\n')):
            p=frame.paragraphs[0] if i==0 else frame.add_paragraph()
            p.text=line;p.font.size=Pt(size);p.font.bold=bold;p.font.name=font;p.font.color.rgb=rgb(color)
            if align:p.alignment=align
        return frame

    def header(slide,main,sub,badge=None):
        text(slide,main,.3,.14,10.4,.45,20,True)
        text(slide,sub,.3,.56,10.4,.28,10,False,ML_UI['muted'])
        bar=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(.3),Inches(.86),Inches(.9),Inches(.04))
        bar.fill.solid();bar.fill.fore_color.rgb=rgb(ML_UI['accent']);bar.line.fill.background()
        if badge:
            label,color,background=badge
            shape=slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,Inches(11.35),Inches(.2),Inches(1.65),Inches(.36))
            shape.fill.solid();shape.fill.fore_color.rgb=rgb(background);shape.line.color.rgb=rgb(color)
            tf=shape.text_frame;tf.text=label;tf.vertical_anchor=MSO_ANCHOR.MIDDLE
            p=tf.paragraphs[0];p.alignment=PP_ALIGN.CENTER;p.font.size=Pt(11);p.font.bold=True;p.font.color.rgb=rgb(color);p.font.name=font
        text(slide,'AUTO REPORT · ML MODE (실험)',9.6,7.2,3.4,.2,8,False,ML_UI['muted'],PP_ALIGN.RIGHT)

    def table(slide,rows,x,y,w,widths,size=9,row_h=.26,hot=None):
        shape=slide.shapes.add_table(len(rows),len(rows[0]),Inches(x),Inches(y),Inches(w),Inches(row_h*len(rows)))
        grid=shape.table
        for j,width in enumerate(widths):grid.columns[j].width=Inches(width)
        for i,row in enumerate(rows):
            grid.rows[i].height=Inches(row_h)
            for j,value in enumerate(row):
                cell=grid.cell(i,j);cell.text=str(value)
                cell.margin_left=cell.margin_right=Inches(.05);cell.margin_top=cell.margin_bottom=Inches(.02)
                cell.fill.solid()
                cell.fill.fore_color.rgb=rgb(ML_UI['subtle'] if i==0 else (ML_UI['accent_bg'] if hot and hot(i-1) else '#ffffff'))
                for p in cell.text_frame.paragraphs:
                    p.font.size=Pt(size);p.font.name=font;p.font.bold=i==0
                    p.font.color.rgb=rgb(ML_UI['muted'] if i==0 else ML_UI['ink'])
        return shape

    def pic(slide,png,x,y,w,h=None):
        if h is None:slide.shapes.add_picture(io.BytesIO(png),Inches(x),Inches(y),width=Inches(w))
        else:slide.shapes.add_picture(io.BytesIO(png),Inches(x),Inches(y),Inches(w),Inches(h))

    overview=prs.slides.add_slide(prs.slide_layouts[6])
    header(overview,'ML Insight 요약',title)
    links=[];item_slides={}
    rows=[['#','항목','상태','선정 이유','인자 신호']]
    for i,e in enumerate(parts_entries[:18],1):
        factor=' / '.join(r['column'] for r in e.get('_factors',{}).get('flagged',[])[:3]) or '-'
        rows.append([i,e['item']+' · '+e['step'],_ml_item_state(e)[0],(e.get('selection_reason') or _ml_finding_summary(e))[:90],factor[:60]])
    summary=table(overview,rows,.3,1.05,12.7,[.4,3.3,1.1,5.3,2.6],9,.28)
    if len(parts_entries)>18:text(overview,f'외 {len(parts_entries)-18}개 항목은 이어지는 페이지에서 확인하세요.',.3,1.1+.28*len(rows),12,.3,10,False,ML_UI['muted'])
    for e in parts_entries:
        state=_ml_item_state(e);render=renders[id(e)];p=render['panels']
        slide=prs.slides.add_slide(prs.slide_layouts[6]);item_slides[id(e)]=slide
        header(slide,e['item'],' · '.join(str(v) for v in (e['vehicle'],e['category'],e['step'],e['program'],e['temperature'],e.get('unit','')) if str(v)),state)
        findings=e.get('ml_findings',[]);factors=e.get('_factors',{})
        facts=[['구분','내용'],
               ['선정 이유',(e.get('selection_reason') or '-')[:150]],
               ['ML 근거',(' / '.join(dict.fromkeys(_ml_module_label(f['module'])+f" (q {f['q']:.2g})" for f in findings)) or 'ML 검정 추가 신호 없음')[:150]],
               ['인자 신호',(' / '.join(r['column']+' · '+' '.join(_ml_signal_label(s) for s in r['signals']) for r in factors.get('flagged',[])) or '연관 신호 없음')[:150]],
               ['자료',f"N={e.get('n',0):,} · lot {e.get('lots',0)} · 신규 lot {e.get('recent_lots',0)} · 검정 {e.get('ml_test_count',0)}회"]]
        table(slide,facts,_ML_LX,.98,_ML_LW,[1.1,_ML_LW-1.1],9,.36)
        pic(slide,p['box'],_ML_LX,2.9,_ML_LW,1.95)
        pic(slide,p['map'],_ML_LX+.1,4.92,_ML_LW-.2,2.3)
        pic(slide,p['trend'],_ML_RX,.98,_ML_RW,1.95)
        pic(slide,p['radius'],_ML_RX,3.0,_ML_RW,1.95)
        pic(slide,p['cdf'],_ML_RX,5.02,_ML_RW,2.2)
        slide.notes_slide.notes_text_frame.text='\n'.join([e.get('selection_reason',''),_ml_finding_summary(e)]+[f['message'] for f in findings]+list(dict.fromkeys(e.get('warnings',[]))))
        # 인자 스크리닝 페이지 — 표(계열별) + 신호 난 인자 차트 2개씩
        charts=render['factors']
        pages=max(1,(max(0,len(charts)-2)+3)//4+1)
        for page in range(pages):
            detail=prs.slides.add_slide(prs.slide_layouts[6])
            header(detail,e['item']+' · ML_TABLE 인자 스크리닝',('KNOB·MASK·EQP = 범주(수준별 비교) / INLINE·VM = 수치(R²·밑둥 들림) · wafer 단위, BH 보정'
                   +(f' · {page+1}/{pages}' if pages>1 else '')),state)
            if page==0:
                frows=_ml_factor_rows(e,8)
                fr=[['계열','인자','종류','지표','q','판정 / 근거']]+[[r[0],r[1],r[2],r[3],r[4],r[5][:95]] for r in frows]
                if len(fr)==1:fr.append(['-','-','-','-','-',('; '.join(factors.get('skipped',[])) or 'ML_TABLE 인자 없음')[:95]])
                table(detail,fr,.3,1.02,12.7,[.8,2.3,.6,2.1,.6,6.3],9,.28,
                      hot=lambda i:i<len(factors.get('rows',[])) and bool(factors['rows'][i]['signals']))
                chunk=charts[:2];top=1.1+.28*len(fr)+.15
                for j,png in enumerate(chunk):pic(detail,png,.3+6.4*j,top,6.3,min(2.55,7.1-top))
                if not charts:text(detail,'신호가 난 인자가 없습니다 — 표의 지표는 참고값입니다.',.3,top+.2,12,.4,12,False,ML_UI['muted'])
            else:
                chunk=charts[2+(page-1)*4:2+page*4]
                for j,png in enumerate(chunk):pic(detail,png,.3+6.4*(j%2),1.05+2.95*(j//2),6.3,2.55)
    for i,e in enumerate(parts_entries[:18]):
        cell=summary.table.cell(i+1,1).text_frame.paragraphs[0]
        if cell.runs:_add_internal_slide_link(cell.runs[0],overview,item_slides[id(e)])
    stream=io.BytesIO();prs.save(stream);return stream.getvalue()


def _ml_report_pack(entries, settings, title):
    """ML mode 발행물 — 항목당 이미지 ≤2장, 메일 1통 이미지·HTML·PPT 한도 안에서 나눈다."""
    entries=sorted(entries,key=_ml_item_priority)
    renders={};vehicle=None
    for i,entry in enumerate(entries,1):
        if entry.get('vehicle')!=vehicle:
            vehicle=entry.get('vehicle')
            try:GLOBAL_CONFIG.load_from_yaml(vehicle)   # 제품별 좌표·설정으로 그린다
            except Exception:pass
        renders[id(entry)]=_ml_item_render(entry,settings)
        if i%10==0:print(f'[INFO] ML mode 항목 차트 {i}/{len(entries)}',flush=True)
    limit=_mail_image_limit(settings)
    html_cap=_html_mail_limit(settings=settings)
    ppt_cap=min(10_000_000,int(settings.get('ppt_max_bytes',10_000_000)))
    cost=lambda e:1+(1 if renders[id(e)]['factor_sheet'] else 0)
    groups=[];current=[]
    for entry in entries:
        if cost(entry)>limit:raise _DailyTrendLimit('ML 항목 1개의 이미지 수가 메일 이미지 한도를 넘습니다. mail_inline_image_limit 을 확인하세요.')
        if current and sum(map(cost,current))+cost(entry)>limit:groups.append(current);current=[]
        current.append(entry)
    if current:groups.append(current)
    parts=[]
    while groups:
        group=groups.pop(0)
        body=_ml_html(group,renders,settings,title,len(parts)+1,len(parts)+1+len(groups),entries)
        ppt=_ml_ppt(group,renders,settings,title)
        if len(body.encode('utf-8'))>=html_cap or len(ppt)>=ppt_cap:
            if len(group)==1:raise _DailyTrendLimit('ML 단일 항목이 메일 용량 한도를 넘습니다.')
            half=len(group)//2;groups[:0]=[group[:half],group[half:]];continue
        parts.append([group,body,ppt])
    if len(parts)>min(10,int(settings.get('max_mail_parts',10))):
        raise _DailyTrendLimit(f'ML 메일이 {len(parts)}통으로 최대 분할 수를 넘습니다. 제품·candidate_source 범위를 줄이세요.')
    result=[]
    for i,(group,body,ppt) in enumerate(parts,1):
        # 번호(메일 i/N)는 최종 분할 수로 다시 그린다.
        body=_ml_html(group,renders,settings,title,i,len(parts),entries)
        body=_fit_html_budget(body, settings=settings)
        images=_assert_inline_images(body)
        if images>limit:raise ValueError(f'ML 메일 본문 이미지 {images}장 > 한도 {limit}장')
        result.append((body,ppt,len(group)))
    print(f'[INFO] ML mode: {len(entries)} items / {len(result)} mail parts / 본문 이미지 ≤ {limit}장/통')
    return result


def _trend_anchor(entry):
    import hashlib,json
    return 'item-'+hashlib.sha256(json.dumps([str(entry.get(k,'')) for k in ('vehicle','category','item','step','program','temperature')],ensure_ascii=False).encode()).hexdigest()[:16]


def _daily_summary(entries, settings):
    import html
    esc=lambda v:html.escape(str(v))
    def when(value):
        try:return pd.Timestamp(value).strftime('%m-%d %H:%M')
        except Exception:return str(value)
    flagged=[e for e in entries if e.get('auto_findings')]
    critical=[e for e in flagged if _trend_review(e)[0]=='이상']
    body=_service_metrics([('24시간 측정 항목·조건',len(entries),'#003366'),('이상',len(critical),'#b4232d'),
                           ('주의',len(flagged)-len(critical),'#b45309'),('이상·주의 없음',len(entries)-len(flagged),'#178A43')])
    body+=('<p style="font-size:13px">하이라이트 구간: <b>'+esc(when(settings.get('highlight_since','')))+' ~ '+esc(when(settings.get('report_now','')))
           +'</b> — 차트의 노란 띠·검정 테두리 점이 이 구간 측정입니다. 판정은 Auto Report 와 같은 분석 함수·제품 설정을 씁니다.</p>')
    body+=_service_heading('이상·주의 항목 · 항목명을 누르면 차트로 이동','daily-findings')
    if not flagged:
        body+='<p style="padding:8px 12px;background:#ecf8ef;color:#178A43;font-weight:700">최근 24시간 측정에서 이상·주의 신호가 없습니다.</p>'
    else:
        # 한 줄 = 한 항목: 판정 배지 · 카테고리 · 항목(링크) · Lot · 근거. 이상을 위로.
        rows=''
        for e in sorted(flagged,key=lambda e:(_trend_review(e)[0]!='이상',e['category'],e['item'])):
            state,color,_=_trend_review(e)
            lots=', '.join(dict.fromkeys(f['lot'] for f in e['auto_findings']))
            basis=' / '.join(dict.fromkeys(f['title'] for f in e['auto_findings']))
            rows+=('<tr><td style="padding:4px 8px;border-bottom:1px solid #e5e5e5"><span style="color:#fff;background:'+color+';padding:1px 7px;font-weight:700;font-size:12px">'+esc(state)+'</span></td>'
                   '<td style="padding:4px 8px;border-bottom:1px solid #e5e5e5;color:#444">'+esc(e['category'])+'</td>'
                   '<td style="padding:4px 8px;border-bottom:1px solid #e5e5e5"><a style="color:#0055aa;font-weight:700" href="#'+_trend_anchor(e)+'">'+esc(e['item'])+'</a>'
                   '<div style="color:#555;font-size:11px">'+esc(' · '.join(str(v) for v in (e['vehicle'],e['step'],e['program'],e['temperature'])))+'</div></td>'
                   '<td style="padding:4px 8px;border-bottom:1px solid #e5e5e5">'+esc(lots)+'</td>'
                   '<td style="padding:4px 8px;border-bottom:1px solid #e5e5e5;color:#333">'+esc(basis[:220])+'</td></tr>')
        body+=('<table cellspacing="0" style="border-collapse:collapse;font-size:13px;width:100%"><tr style="background:#e8edf3;color:#003366;text-align:left">'
               +''.join('<th style="padding:5px 8px">'+h+'</th>' for h in ('판정','카테고리','항목','Lot','근거'))+'</tr>'+rows+'</table>')
    skipped=[r for r in settings.get('_coverage',[]) if r['status']!='ok']
    if skipped:body+='<p style="color:#8a3800">발행 생략: '+esc(' / '.join(r['vehicle']+': '+r.get('reason',r['status']) for r in skipped))+'</p>'
    return body


def _mail_image_limit(settings=None):
    """메일 1통 본문 <img> 상한. 사내 메일 API 는 본문 인라인 이미지를 첨부로 떼어 세는 경우가 있어
    '첨부 + 이미지'가 mail_attach_limit(기본 10)을 넘으면 'Attach file count is over 10' 으로 발송을 거부한다.
    기본 = 한도 − PPT 1개 − 여유 1개 = 8장."""
    settings=settings or {}
    attach=int(settings.get('mail_attach_limit') or GLOBAL_CONFIG.get('mail_attach_limit',10) or 10)
    value=settings.get('mail_image_limit') or GLOBAL_CONFIG.get('mail_inline_image_limit') or attach-2
    return max(1,min(int(value),attach-1))


def _daily_strip_uri(entries, columns, settings, width=1320):
    """한 줄의 Daily 차트들을 가로 띠 이미지 1장으로(칸 폭 = width/columns). 띠 단위로 캐시한다."""
    import io
    from PIL import Image
    cache=settings.setdefault('_strip_cache',{})
    key=tuple(id(e) for e in entries)+(columns,)
    if key in cache:return cache[key]
    cap=int(getattr(GLOBAL_CONFIG,'html_inline_img_max_kb',100) or 100)*1024
    charts=[Image.open(io.BytesIO(e['png'])).convert('RGB') for e in entries]
    spatial=len(entries)==1 and ' / spatial / ' in str(entries[0].get('item',''))
    cell=width if spatial else width//columns
    for attempt in range(6):
        scaled=[c.resize((cell,max(1,round(c.height*cell/c.width))),Image.Resampling.LANCZOS) if c.width>cell else c for c in charts]
        canvas=Image.new('RGB',(sum(max(cell,c.width) for c in scaled),max(c.height for c in scaled)),'white')
        x=0
        for c in scaled:canvas.paste(c,(x,0));x+=max(cell,c.width)
        packed=io.BytesIO()
        canvas.quantize(colors=256,method=Image.Quantize.FASTOCTREE,dither=Image.Dither.NONE).save(packed,format='PNG',optimize=True)
        if packed.tell()<=cap:break
        cell=int(cell*.88)
    else:
        raise _DailyTrendLimit('차트 한 줄 이미지가 인라인 이미지 한도를 초과했습니다. html_columns 또는 항목 범위를 조정해 주세요.')
    uri=_img_datauri(packed.getvalue());cache[key]=uri
    return uri


def _daily_trend_pack(entries, settings, title):
    """Try one PPT/mail first; split by measured PPT/HTML bytes, never by dropping items."""
    if settings.get('service')=='mlmode' and entries:
        owners=[e for e in entries if not e.get('parent_item')]
        for e in owners:e.setdefault('_factors',dict(rows=[],flagged=[],skipped=[],families={}))
        return _ml_report_pack(owners,settings,title)
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
        flagged=[e for e in batch if e.get('auto_findings')]
        summary_links=[];item_slides={}
        for start in range(0,max(1,len(flagged)),6):
            overview=prs.slides.add_slide(prs.slide_layouts[6])
            tf=overview.shapes.add_textbox(Inches(.4),Inches(.25),Inches(12.4),Inches(.5)).text_frame
            tf.text='Daily Trend · 최근 24시간 이상·주의';tf.paragraphs[0].font.size=Pt(22)
            tf=overview.shapes.add_textbox(Inches(.4),Inches(.9),Inches(12.4),Inches(.55)).text_frame
            tf.text=str(settings.get('highlight_since',''))+' ~ '+str(settings.get('report_now',''))+' / Auto Report 공통 판정'
            tf.paragraphs[0].font.size=Pt(12)
            for j,e in enumerate(flagged[start:start+6]):
                tf=overview.shapes.add_textbox(Inches(.5),Inches(1.6+j*.84),Inches(12.1),Inches(.78)).text_frame
                tf.word_wrap=True
                tf.text=e['vehicle']+' / '+e['category']+' / '+e['item']+' / '+e['step']+' / '+e['program']+' / '+str(e['temperature'])+' — '+_trend_review(e)[0]
                tf.paragraphs[0].font.size=Pt(15)
                summary_links.append((tf.paragraphs[0].runs[0],overview,_trend_anchor(e)))
                p=tf.add_paragraph();p.text='Lot: '+', '.join(dict.fromkeys(f['lot'] for f in e['auto_findings']))+' · '+', '.join(dict.fromkeys(f['title'] for f in e['auto_findings']))
                p.font.size=Pt(10)
            if not flagged:
                tf=overview.shapes.add_textbox(Inches(.5),Inches(1.8),Inches(12),Inches(1)).text_frame
                tf.text='최근 24시간 측정에서 Auto Report 기준 이상·주의 신호 없음\n카테고리별 전체 측정 추이를 다음 페이지에서 확인하세요.'
                for p in tf.paragraphs:p.font.size=Pt(18)
        rows=[];cards=[];categories={};overflow=[]
        for i,entry in enumerate(batch):
            if ml_mode or i%2==0:
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                tf=slide.shapes.add_textbox(Inches(.3),Inches(.08),Inches(12.7),Inches(.42)).text_frame
                tf.text=title;tf.paragraphs[0].font.size=Pt(18)
            item_slides[_trend_anchor(entry)]=slide
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
                    evidence=entry.get('auto_findings',[])
                    detail=(evidence[0].get('basis') or evidence[0]['title']) if evidence else '최근 24시간 이상·주의 신호 없음'
                    info.text=state+'\n'+f"전체 {entry['lots']} lots / 24시간 {entry.get('recent_lots',0)} lots\n"+str(detail)
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
            # 메일 본문 그림은 '한 줄(html_columns 칸) = 이미지 1장' 띠로 합친다(아래 _daily_strip_uri).
            # 사내 메일 API 는 본문 인라인 이미지를 첨부로 떼어 첨부 10개 한도에 셀 수 있다(Attach file count is over 10).
            if settings.get('service')=='daily_trend' and len(entry['png'])>int(getattr(GLOBAL_CONFIG,'html_inline_img_max_kb',100) or 100)*1024:
                raise _DailyTrendLimit('차트 한 장이 인라인 이미지 한도를 초과했습니다. 항목/범위를 조정해 주세요. 화질은 자동으로 낮추지 않습니다.')
            state,color,_=_trend_review(entry)
            reading='<div style="padding:8px 10px;font-size:12px;line-height:1.6"><b style="color:'+color+'">'+html.escape(state)+'</b> · 최근 24시간 '+str(entry.get('recent_lots',0))+' lots / '+str(entry.get('recent_n',0))+'점'
            if entry.get('auto_findings'):
                reading+='<ul style="margin:4px 0;padding-left:18px">'+''.join('<li>'+html.escape(f['lot']+' · '+f['title']+' — '+str(f.get('detail','')))+'</li>' for f in entry['auto_findings'])+'</ul>'
            if entry['warnings']:reading+='<p style="color:#8a3800;margin:4px 0">'+html.escape(' / '.join(dict.fromkeys(entry['warnings'])))+'</p>'
            reading+='</div>'
            anchor=_trend_anchor(entry)
            # 항목명을 크게, 조건(카테고리·Step·프로그램·온도·단위)은 작게, 판정은 색 배지로 — 격자에서도 무슨 차트인지 바로 읽힌다.
            header=('<div id="'+anchor+'" style="padding:5px 8px;background:#e8edf3;border:1px solid #cbd5df"><a name="'+anchor+'"></a>'
                    '<span style="float:right;font-size:11px;font-weight:700;color:#fff;background:'+color+';padding:1px 6px">'+html.escape(state.split(' ')[0])+'</span>'
                    '<div style="color:#003366;font-size:15px;font-weight:700;line-height:19px">'+html.escape(entry['item'])+'</div>'
                    '<div style="color:#444;font-size:11px;line-height:15px">'+html.escape(' · '.join(str(v) for v in (entry['category'],entry['step'],entry['program'],entry['temperature'],entry.get('unit','')) if str(v)))+'</div></div>')
            categories.setdefault((entry['vehicle'],entry['category']),[]).append(dict(header=header,reading=reading,spatial=spatial,entry=entry,label=label))
        for entry,rest in overflow:
            for start in range(0,len(rest),18):
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                tf=slide.shapes.add_textbox(Inches(.4),Inches(.1),Inches(12),Inches(.5)).text_frame
                tf.text=entry['item']+' / legend continued';tf.paragraphs[0].font.size=Pt(18)
                spatial=' / spatial / ' in entry['item']
                slide.shapes.add_picture(io.BytesIO(entry.get('_ppt_png',entry['png'])),Inches(.4),Inches(.9),width=Inches(7.2),height=Inches(7.2*3.6/12.4 if spatial and ml_mode else (3.2 if ml_mode else 2.65)))
                draw_legend(slide,rest[start:start+18],9.5 if ml_mode else 7.8,.9)
        body=_service_html_start(title,'이상 후보의 탐지 근거와 후속 검토' if ml_mode else '',str(settings.get('report_now','')))
        body+=_daily_summary(batch,settings)
        body+='<nav style="padding:8px 0;border-bottom:1px solid #e0e0e0">'+ ' &nbsp; '.join('<a style="color:#0f62fe;font-size:14px;display:inline-block;padding:4px 8px" href="#cat'+str(i)+'">'+html.escape('/'.join(key))+' ('+str(len(group))+')</a>' for i,(key,group) in enumerate(categories.items()))+'</nav>'
        columns=max(1,min(3,int(settings.get('html_columns',3))))
        images=0
        for i,(key,group) in enumerate(categories.items()):
            body+='<div class="trend-category"><h2 id="cat'+str(i)+'" style="position:sticky;top:0;z-index:5;font-size:16px;font-weight:600;background:'+category_fill[key]+';border-left:3px solid #0f62fe;padding:8px;margin:16px 0 4px">'+html.escape(' / '.join(key))+' · '+str(len(group))+' <a href="#top" style="float:right;color:#0f62fe;font-size:12px;font-weight:400">목록 ↑</a></h2><table role="presentation" width="100%" style="table-layout:fixed;border-spacing:4px 0">'
            # 한 줄 = 머리글 칸들 / 차트 띠(이미지 1장) / 판정 칸들. 공간 상세는 한 줄을 혼자 쓴다.
            lines=[];line=[]
            for card in group:
                if card['spatial'] or len(line)==columns:
                    if line:lines.append(line)
                    line=[]
                line.append(card)
                if card['spatial']:lines.append(line);line=[]
            if line:lines.append(line)
            for line in lines:
                spans=[columns if c['spatial'] else 1 for c in line]
                used=sum(spans);pad='<td colspan="'+str(columns-used)+'"></td>' if used<columns else ''
                body+='<tr>'+''.join('<td colspan="'+str(n)+'" width="'+str(100*n/columns)+'%" style="vertical-align:top;padding-top:8px">'+c['header']+'</td>' for c,n in zip(line,spans))+pad+'</tr>'
                uri=_daily_strip_uri([c['entry'] for c in line],columns,settings)
                label=' | '.join(c['label'] for c in line)
                body+=('<tr><td colspan="'+str(columns)+'" style="padding:0"><img alt="'+html.escape(label,quote=True)+'" width="'+str(int(1000*used/columns))+
                       '" style="display:block;width:'+str(round(100*used/columns,3))+'%;height:auto" src="'+uri+'"></td></tr>')
                images+=1
                body+='<tr>'+''.join('<td colspan="'+str(n)+'" style="vertical-align:top;border:1px solid #cbd5df;border-top:0;background:white">'+c['reading']+'</td>' for c,n in zip(line,spans))+pad+'</tr>'
            body+='</table></div>'
        body+='</div></body></html>'
        _assert_inline_images(body, images)
        _service_deck_style(prs)
        from My_Function import _add_internal_slide_link
        for run,source,anchor in summary_links:_add_internal_slide_link(run,source,item_slides[anchor])
        stream=io.BytesIO();prs.save(stream);ppt=stream.getvalue()
        return body,ppt
    image_limit=_mail_image_limit(settings)
    def fits(body,ppt):
        # Independent artifact limits: inline base64 is already included in HTML bytes.
        # 본문 이미지 수도 한도 — 넘으면 다음 메일로 나눈다(메일 API 첨부 개수 제한).
        return (len(ppt)<min(10_000_000,int(settings.get('ppt_max_bytes',10_000_000))) and
                len(body.encode('utf-8'))<_html_mail_limit(settings=settings) and
                len(re.findall(r'<img\s',body))<=image_limit)
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
    parts=[(_fit_html_budget(body, settings=settings),ppt,count) for body,ppt,count in parts]
    print(f'[INFO] HTML 인라인 이미지 검증 OK / {len(entries)} charts / {len(parts)} parts')
    return parts


def _daily_trend_report(request):
    import hashlib,json
    from report_items import load_catalog, select_formatter
    settings=dict(request['settings']);products=settings.get('products') or []
    if request.get('send') and not settings.get('recipients'):
        return dict(status='disabled',reason='지정 수신처 없음; 발행 생략')
    service=request.get('service','daily_trend')
    if service not in ('daily_trend','mlmode'):raise ValueError('Unknown report service')
    items_path=settings.get('items_file') or os.path.join(GLOBAL_CONFIG.base_path,'reformatter','report_items.yaml')
    settings['_report_items_catalog']=load_catalog(items_path)
    if not products:raise ValueError('daily_trend.products에 선택 제품을 지정하세요')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+',str(p)) for p in products):raise ValueError('잘못된 제품 키')
    identity=request['id']+'-'+hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()[:12]
    dest=os.path.join(operations_root(),service,re.sub(r'[^A-Za-z0-9_.-]','_',identity))
    settings['service']=service
    settings['report_now']=pd.Timestamp.fromtimestamp(request['now'])
    settings['highlight_since']=pd.Timestamp(settings.get('highlight_since') or (settings['report_now']-pd.Timedelta(days=1)))
    # Separate checkpoint per service/source selection, unaffected by daily date or recipients.
    checkpoint_key=service+'|'+hashlib.sha256(json.dumps([products,settings.get('with_vehicle',{}),settings['_report_items_catalog'].get(service,{})],sort_keys=True).encode()).hexdigest()
    checkpoint=ops_get('trend_publication_checkpoints',checkpoint_key,{})
    settings['_published_observations']=set(checkpoint['observations']) if 'observations' in checkpoint else None
    settings['_candidate_observations']=set()
    os.makedirs(dest,exist_ok=True)
    manifest_path=os.path.join(dest,'manifest.json')
    # 소스 지문: ML_TABLE 변경·기간 변경 시 동결 산출물을 재사용하지 않고 재빌드한다.
    def _source_fingerprint():
        fp={'highlight_since':str(settings.get('highlight_since')), 'products':list(products),
            'report_items':settings['_report_items_catalog'].get(service,{})}
        for _v in dict.fromkeys(products):
            _p=os.path.join(settings.get('ml_table_dir') or 'RUN/DB', f'ML_TABLE_{_v}.parquet')
            try:_st=os.stat(_p);fp[_v]=dict(mtime=_st.st_mtime, size=_st.st_size)
            except OSError:fp[_v]=None
        return fp
    _current_fp=_source_fingerprint()
    manifest=None
    if os.path.exists(manifest_path):
        with open(manifest_path,encoding='utf-8') as stream:manifest=json.load(stream)
        if manifest.get('source_fingerprint')!=_current_fp:
            print('[INFO] Daily Trend 소스 변경 감지(ML_TABLE·기간) → 저장 산출물을 재빌드합니다.')
            manifest=None
        else:
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
            ml_path=os.path.join(settings.get('ml_table_dir') or GLOBAL_CONFIG.get('DB') or 'RUN/DB',f'ML_TABLE_{vehicle}.parquet')
            if not os.path.isfile(ml_path):
                coverage.append(dict(vehicle=vehicle,status='missing_ml_table',items=0,reason=f'ML_TABLE_{vehicle}.parquet 없음'))
                continue
            formatter=pd.read_csv(os.path.join('reformatter',vehicle+'_reformatter.csv'))
            coordinate_path=GLOBAL_CONFIG.get('coordinate_file_path')
            if coordinate_path and os.path.exists(coordinate_path):
                set_chip_layout(pd.read_excel(coordinate_path,sheet_name='Zone_Define'))
            selected,_=select_formatter(formatter,vehicle,service,settings['_report_items_catalog'])
            if selected.empty:
                coverage.append(dict(vehicle=vehicle,status='no_items',items=0));continue
            frame=daily_trend_load(vehicle,formatter,int(GLOBAL_CONFIG.get('viewing_period',30))+2)
            product_entries=daily_trend_entries(frame,formatter,vehicle,settings,pd.Timestamp.fromtimestamp(request['now']))
            if service=='daily_trend':
                product_entries=[e for e in product_entries if e.get('recent_n',0)>0]
                product_entries=daily_auto_findings(product_entries,formatter)
            elif vehicle in products and _ml_candidate_source(settings)!='ml':
                # ML 은 Daily Trend 가 이상·주의로 본 항목을 자세히 파고든다 → 같은 판정 함수로 후보 표시.
                product_entries=daily_auto_findings(product_entries,formatter)
            source_entries[vehicle]=product_entries
            coverage.append(dict(vehicle=vehicle,status='ok' if product_entries else 'no_recent_data',items=len(product_entries),
                                 viewing_period=GLOBAL_CONFIG.get('viewing_period',30)))
        if not any(source_entries.get(p) for p in products):
            result=dict(id=identity,status='skipped',reason='ML_TABLE 없음 또는 대상 측정 없음',created=request['now'],
                        preview_only=not request.get('send',False),coverage=coverage,items=0,parts=[])
            ops_put(service+'_reports',identity,result)
            return result
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
            # candidate_source: daily=Daily Trend 이상·주의 항목만 ML 상세 분석(가장 가볍다) /
            #                   ml=모든 항목을 ML 로 선별(예전 방식) / either=둘 중 하나라도 해당(기본)
            source=_ml_candidate_source(settings)
            pool=[e for e in entries if e.get('auto_findings')] if source=='daily' else entries
            entries,analysis=ml_trend_select(pool,settings)
            chosen={id(e) for e in entries}
            if source in ('daily','either'):
                for e in pool:
                    if e.get('auto_findings') and id(e) not in chosen:
                        e.setdefault('warnings',[]).append('ML 검정에서는 추가 근거 없음 — Daily Trend 판정으로 포함')
                        entries.append(e)
            for e in entries:
                reasons=[]
                if e.get('auto_findings'):
                    reasons.append('Daily 판정: '+', '.join(dict.fromkeys(f['title'] for f in e['auto_findings']))[:160])
                if e.get('ml_findings'):
                    reasons.append('ML: '+', '.join(dict.fromkeys(_ml_module_label(f['module']) for f in e['ml_findings'])))
                e['selection_reason']=' · '.join(reasons)
            analysis['candidate_source']=source
            if settings.get('influence_enabled',True):analysis['influence']=ml_influence_analyze(entries,settings)
            else:
                for entry in entries:entry['_influence']=dict(candidates=[],skipped=[])
            # ML_TABLE 인자 스크리닝(KNOB·MASK·EQP 범주 / INLINE·VM 수치 R²·밑둥 들림) — 리포트 본문의 인자 차트 근거.
            if settings.get('factor_screen_enabled',True):analysis['factors']=ml_factor_screen(entries,settings)
        settings['_coverage']=coverage;settings['_analysis']=analysis
        # ML: 같은 카테고리 안에서 ML 기법이 잡은 항목을 먼저(가장 볼 가치가 큰 것부터).
        entries.sort(key=lambda e:(e['vehicle'],e['category'],service=='mlmode' and not e.get('ml_findings'),e['item'],e['step'],e['program'],e['temperature']))
        plot_entries=[]
        _chart_vehicle=None
        for i,entry in enumerate(entries,1):
            # 제품별 override(viewing_period·집계·밴드 등)가 차트에 섞이지 않도록 제품별 config로 렌더
            if entry.get('vehicle')!=_chart_vehicle:
                _chart_vehicle=entry.get('vehicle')
                try:GLOBAL_CONFIG.load_from_yaml(_chart_vehicle)
                except Exception:pass
            # ML mode 는 _ml_report_pack 이 항목 페이지 차트를 직접 그린다(여기서 그리면 버려지는 그림).
            if service!='mlmode':entry['png']=_daily_trend_chart(entry,settings)
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
            content='<html><meta charset="utf-8"><body><h2>Daily Trend 발행 범위를 조정해 주세요</h2><p>'+html.escape(str(exc))+'</p><p>제품: '+html.escape(', '.join(products))+' / 차트: '+str(len(plot_entries))+'</p><p>HTML 본문은 1MB 이하, PPTX 첨부는 별도로 10MB 이하, 한 번의 발행은 최대 10통입니다. My_config.py의 daily_trend 설정과 제품 reformatter의 카테고리/항목 선택을 조정해 주세요.</p></body></html>'
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
            row={k:v for k,v in entry.items() if k not in ('points','png','spatial','_inline_uri','_ppt_png','_legend_rows','_influence','_factors')}
            row['knob_values']='; '.join(sorted(entry['points']['_knob'].unique())) if not entry['points'].empty else ''
            catalog.append(row)
        atomic_bytes(os.path.join(dest,'catalog.csv'),pd.DataFrame(catalog).to_csv(index=False).encode('utf-8-sig'))
        if service=='mlmode':
            audit=[dict(vehicle=e['vehicle'],item=e['item'],step=e['step'],program=e['program'],temperature=e['temperature'],analysis=e.get('_influence',{}),
                        # 인자 스크리닝 결과(차트용 점 목록은 빼고 수치만) — 리포트 표와 같은 값을 파일로 대조할 수 있게.
                        factors=[{k:v for k,v in r.items() if k!='plot'} for r in e.get('_factors',{}).get('rows',[])],
                        factor_skipped=e.get('_factors',{}).get('skipped',[])) for e in entries]
            atomic_bytes(os.path.join(dest,'influence.json'),json.dumps(audit,ensure_ascii=False,indent=2,default=str).encode('utf-8'))
        manifest=dict(id=identity,parts=parts,notice=notice,summary_artifact=summary_artifact,coverage=coverage,items=len(entries),detail_panels=len(plot_entries)-len(entries),created=request['now'],analysis=analysis,code_version=_CODE_VERSION,
                      checkpoint_key=checkpoint_key,observations=sorted(settings['_candidate_observations']),source_fingerprint=_current_fp)
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
        result['status']='no_findings' if manifest['analysis'].get('tested_items',manifest['analysis'].get('statistical_tests',0)) and not manifest['analysis'].get('budget_limited') else 'insufficient_data'
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
    if status=='skipped':
        return report,'조건 제외 / 자료 없음',reason or '대상 측정과 발행 조건 확인',False
    if report.get('generated') and report.get('saved'):
        if email=='sent':return report,'발행 완료','추가 조치 없음',False
        if email=='disabled':return report,'저장 완료 · 발송 꺼짐','메일 사용 설정에 따른 미발송',False
        return report,'저장 완료 · 메일 확인','메일 사용 설정과 전송 결과 확인',True
    if not report and any(token in reason for token in ('=False','=True','제외','조건 미충족')):
        return report,'설정 제외 / 측정 대기','제외 설정 또는 측정 완료 조건 확인',False
    return report,'발행 이력 미확인','최신 측정의 발행 대상 여부와 실행 로그 확인',True


def _watchdog_overview(health, action_rows, relevant, publication, reports, scheduler_runs, start, now, settings):
    """Watchdog 메일 맨 위 '한눈에': 판정 · 서버 상태 · 제품별 24시간 · 발행 내역(시간순)."""
    import html, json, shutil
    esc=lambda v:html.escape(str(v))
    ok=health.get('state')=='healthy' and not action_rows
    color='#178A43' if ok else '#b4232d'
    verdict=('정상 — Scheduler 동작, 측정 확인, 리포트 발행에 확인할 일이 없습니다.' if ok else
             f'확인 필요 {len(action_rows)}건 — 아래 "우선 확인" 표부터 보세요.')
    out=['<div style="margin:10px 0 6px;padding:10px 14px;border-left:6px solid '+color+';background:'+('#ecf8ef' if ok else '#fdeeee')+'">'
         '<div style="font-size:18px;font-weight:700;color:'+color+'">'+('정상' if ok else '확인 필요')+'</div>'
         '<div style="font-size:13px;color:#222">'+esc(verdict)+' · '+esc(health.get('message',''))+'</div></div>']
    # 서버 상태 — 같은 서버에서 도는 다른 작업(S3 전송 등)까지 반영된 실측값
    server=[]
    try:
        import resource_governor as governor
        cores=governor.usable_cores();busy=governor.busy_cores(cores,sample_sec=.2);avail=governor.available_memory_gb()
        server+=[('CPU',f'{cores}코어 · 사용 {busy:.1f}'),('가용 메모리','측정 불가' if avail is None else f'{avail:.1f} GB')]
    except Exception:
        pass
    try:
        disk=shutil.disk_usage(operations_root());server.append(('디스크 여유',f'{disk.free/1e9:.0f} GB ({disk.free/disk.total*100:.0f}%)'))
    except OSError:
        pass
    beat_age=None
    try:
        with open(os.path.join(operations_root(),'scheduler_heartbeat.json'),encoding='utf-8') as stream:
            beat=json.load(stream)
        beat_age=now-float(beat.get('updated',now))
        server.append(('Scheduler 마지막 응답',datetime.fromtimestamp(float(beat.get('updated',now))).strftime('%m-%d %H:%M')+f' ({beat_age/60:.0f}분 전)'))
    except (OSError,ValueError):
        server.append(('Scheduler 마지막 응답','기록 없음'))
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'RUN','QUEUE','scheduler_state.json'),encoding='utf-8') as stream:
            state=json.load(stream)
        server.append(('대기 중 수동 요청',f"{len(state.get('pending',[]))}건 · 누적 순회 {state.get('cycle',0)}회"))
    except (OSError,ValueError):
        pass
    failed_runs=sum(r.get('rc')!=0 for r in scheduler_runs)
    server.append(('24시간 실행',f'{len(scheduler_runs)}회 · 실패 {failed_runs}회'))
    out.append(_service_heading('서버 상태','server')+'<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px"><tr>'
               +''.join('<td style="border:1px solid #d0d7de;background:#f6f8fa;vertical-align:top"><div style="color:#555;font-size:11px">'+esc(k)+'</div><b>'+esc(v)+'</b></td>' for k,v in server)+'</tr></table>')
    # 제품별 24시간 — 측정 확인 → 발행 → 소요 시간을 한 줄로
    rows=[]
    window=[r for r in reports if start<=r.get('started',0)<=now]
    products=sorted(set(settings.get('products',[]))|{m.get('vehicle','') for m in relevant.values()}|{r.get('vehicle','') for r in window})
    for product in filter(None,products):
        mine=[r for r in window if r.get('vehicle')==product]
        checked=[pk for pk,m in relevant.items() if m.get('vehicle')==product]
        attention=sum(publication[pk][3] for pk in checked)
        runs=[r for r in scheduler_runs if str(r.get('vehicle','')).split()[0:1]==[product] or str(r.get('vehicle',''))==product]
        elapsed=[float(r.get('elapsed',0)) for r in runs if r.get('elapsed')]
        last=max((r.get('updated',r.get('started',0)) for r in mine),default=0)
        state='확인 필요' if attention or any(r.get('rc')!=0 for r in runs) else ('정상' if checked or mine or runs else '기록 없음')
        rows.append([product,state,len(checked),sum(r.get('email')=='sent' for r in mine),
                     sum(r.get('status')=='success' and r.get('email')!='sent' for r in mine),
                     sum(r.get('status') in ('failed','unknown') for r in mine),
                     f'{len(runs)}회 / 평균 {sum(elapsed)/len(elapsed)/60:.1f}분' if elapsed else f'{len(runs)}회',
                     datetime.fromtimestamp(last).strftime('%m-%d %H:%M') if last else '-'])
    body=''.join('<tr>'+''.join('<td style="border-bottom:1px solid #e5e5e5;padding:5px 8px;'+('font-weight:700;color:'+('#178A43' if v=='정상' else '#b4232d' if v=='확인 필요' else '#555')+';' if j==1 else '')+'">'+esc(v)+'</td>' for j,v in enumerate(row))+'</tr>' for row in rows)
    out.append(_service_heading('제품별 24시간 현황','overview')+'<table cellspacing="0" style="border-collapse:collapse;font-size:13px"><tr style="background:#e8edf3;color:#003366">'
               +''.join('<th style="padding:5px 8px;text-align:left">'+h+'</th>' for h in ['제품','판정','측정 확인','메일 발행','생성만','실패·확인','순회 실행 / 평균 소요','마지막 처리'])+'</tr>'+(body or '<tr><td colspan="8" style="padding:6px">기록 없음</td></tr>')+'</table>')
    # 발행 내역 — 최근 순서로 20건
    timeline=sorted(window,key=lambda r:-(r.get('updated') or r.get('started') or 0))[:20]
    out.append(_service_heading('리포트 발행 내역 (최근 20건)','timeline')+_service_table(['시각','제품','Lot / Step','구분','결과','메일','소요'],
        [[datetime.fromtimestamp(r.get('updated') or r.get('started')).strftime('%m-%d %H:%M'),r.get('vehicle',''),
          f"{r.get('lot','')} / {r.get('step','')}",'자동' if r.get('mode','AUTO')=='AUTO' else '수동('+str(r.get('mode'))+')',
          r.get('status',''),r.get('email','') or '-',f"{float(r.get('elapsed',0))/60:.1f}분" if r.get('elapsed') else '-'] for r in timeline]))
    out.append('<p style="color:#555;font-size:12px;margin-top:14px;border-top:1px solid #d0d7de;padding-top:8px">아래는 상세 기록입니다. 평소에는 위 요약만 보면 됩니다.</p>')
    return ''.join(out)


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
          '집계: '+datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M')+' ~ '+datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')+' (운영 서버 시각)'),
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
    body.append(_watchdog_overview(health,action_rows,relevant,publication,reports,scheduler_runs,start,now,settings))
    body.append(_service_metrics([('확인 Prime key',len(relevant),'#003366'),('신규·갱신 측정',len(new),'#003366'),
                 ('최신 측정 저장 완료',sum(bool(r.get('generated') and r.get('saved')) for r,_,_,_ in publication.values()),'#003366'),
                 ('확인 필요 항목',len(action_rows),'#b4232d' if action_rows else '#003366')]))
    body.append('<p>Heartbeat와 발행 결과는 별도 상태입니다. 설정 제외·측정 대기는 실패로 세지 않으며, 발송 확인 필요 건은 수신 여부 확인 후 재발송하세요.</p>')
    body.append('<p><a href="#products" style="color:#0055aa">제품별 현황</a> &nbsp; <a href="#publications" style="color:#0055aa">최신 측정 발행 결과</a> &nbsp; <a href="#execution" style="color:#0055aa">실행 로그</a></p>')
    product_rows=[]
    for product in sorted(set(settings.get('products',[])) | {r.get('vehicle','') for r in runs} | {r.get('vehicle','') for r in scheduler_runs} | {r.get('vehicle','') for r in relevant.values()}):
        checked=[m for m in relevant.values() if m.get('vehicle')==product]
        executions=[r for r in scheduler_runs if r.get('vehicle')==product]
        failures=sum(r.get('rc')!=0 for r in executions)
        status='실패 확인' if failures else ('실행 완료 확인' if executions else '구간 내 완료 이력 없음')
        checked_reports=[publication[m['prime_key']][0] for m in checked]
        generated=sum(bool(r.get('generated')) for r in checked_reports)
        from collections import Counter
        states=Counter(publication[m['prime_key']][1] for m in checked)
        reasons=Counter(m.get('reason','사유 없음') for m in checked if publication[m['prime_key']][0].get('email')!='sent')
        attention=sum(publication[m['prime_key']][3] for m in checked)
        sent_count=sum(r.get('email')=='sent' for r in checked_reports)
        outcome='확인 필요' if failures or attention else ('정상 처리' if checked else '확인 이력 없음')
        product_rows.append([product,status,outcome,sent_count,len(checked),
                             ' / '.join(f'{key} {value}건' for key,value in states.items()),
                             ' / '.join(f'{key} ({value}건)' for key,value in reasons.items()) or ('모두 메일 발송 완료' if checked else '구간 내 확인 이력 없음'),
                             f'실행 {len(executions)} / 실패 {failures} / 생성 {generated} / 저장 {sum(bool(r.get("saved")) for r in checked_reports)}',attention])
    body+=[_service_heading('제품별 실행 및 최신 측정 발행 현황','products'),table(['제품','실행 상태','종합','메일 성공','확인 대상','발행 / 제외 / 대기','미발송 조건·사유','처리 건수','확인 필요'],product_rows)]
    body.append(_service_heading('우선 확인 · 운영 조치','actions')+table(['제품 / 대상','상태','근거','다음 확인'],action_rows))
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
                     m.get('run_id',''),m.get('log_path',''),m.get('reason',''),r.get('mode','AUTO'),action])
    headers=['제품','Prime key','Lot','Step','측정시각','생성','저장','메일','시도','시간','상태','사유','단계별 소요','로그 확인시각','Run ID','로그 파일','발행 대상 조건','발행 모드','다음 확인']
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


def _execution_wait_sec():
    import math
    value = float(os.getenv('AUTO_REPORT_EXECUTION_WAIT_SEC',
                           str(GLOBAL_CONFIG.get('execution_lock_wait_sec', 10800))))
    if not math.isfinite(value) or value < 0:
        raise ValueError('execution_lock_wait_sec는 유한한 0 이상의 초여야 합니다')
    return value


def _execute_serially(action):
    """모든 무거운 작업을 직렬화하고 렌더 워커까지 종료한 뒤 잠금을 반납한다."""
    path = os.path.join(operations_root(), 'locks', 'executor.lock')
    with process_lock(path, wait_sec=_execution_wait_sec()):
        try:
            return action()
        finally:
            # 다른 제품 작업이 시작될 때 이전 작업의 워커·메모리가 남지 않아야 한다.
            try:
                _drain_uploads(block=True)
            finally:
                shutdown_chart_pool()


def _main_cli():
    global _RUN
    argument=sys.argv[1] if len(sys.argv)>1 else ''
    if argument=='--mlmode-evaluate':
        destination=os.path.join(operations_root(),'mlmode_evaluation',datetime.now().strftime('%Y%m%d-%H%M%S'))
        report=_execute_serially(lambda: mlmode_evaluate(dict(GLOBAL_CONFIG.mlmode),destination))
        print(f"[INFO] ML Lab 가상 데이터 검증 완료: {destination} / {report['ensemble']}")
        return 0
    if argument=='--daily-trend-report':
        import json
        with open(sys.argv[2],encoding='utf-8') as stream:request=json.load(stream)
        result=_execute_serially(lambda: _daily_trend_report(request))
        return 0 if result['status'] in ('sent','preview','no_findings','insufficient_data','disabled','skipped') else 1
    if argument=='--watchdog-report':
        import json
        with open(sys.argv[2],encoding='utf-8') as stream:request=json.load(stream)
        result=_watchdog_report(request)
        return 0 if result['status'] in ('sent','preview','disabled') else 1
    command = _parse_command(sys.argv[1:])
    argument = command['argument']
    _RUN=OperationRun(argument)
    def execute():
        _,vehicle,_,_,_=_parse_trigger(argument)
        lock_name=re.sub(r'[^A-Za-z0-9_.-]','_',vehicle)
        wait=float(getattr(GLOBAL_CONFIG,'product_lock_wait_sec',3600) or 0)
        with process_lock(os.path.join(operations_root(),'locks',lock_name+'.lock'),wait_sec=wait):
            _RUN.stage('startup')
            _main_impl(command)
            _drain_uploads(block=True)
            return _RUN.finish()
    try:
        if command['kind'] == 'init_db':
            _RUN.stage('waiting_product')
            return execute()
        _RUN.stage('waiting_execution')
        return _execute_serially(execute)
    except BaseException as exc:
        try:_drain_uploads(block=True)
        except Exception:pass
        _RUN.finish(exc)
        raise


def main():
    global _CODE_VERSION
    if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
        return _main_cli()
    from runtime_versions import runtime_lease, snapshot
    root = os.path.dirname(os.path.abspath(__file__))
    with runtime_lease(root):
        version = snapshot(root)
        _CODE_VERSION = version['id']
        print(f"[VERSION] Auto Report {_CODE_VERSION} / {version.get('created_at', '')} / {version.get('label', '')}", flush=True)
        return _main_cli()


if __name__ == "__main__":
    sys.exit(main() or 0)
