# ----- Python 표준 라이브러리
import gc
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, date
import builtins
import atexit
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
from anomaly_engine import run_anomaly_pipeline, analyze_commonality, render_findings_html, interpret_with_ai, item_excluded

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

    if len(sys.argv) != 2:
        print("Usage: python main.py <ItemName>")
        sys.exit(1)

    raw_arg = sys.argv[1]
    trigger_flag = False

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
    process_id = GLOBAL_CONFIG.get("process_id")
    with_vehicle = GLOBAL_CONFIG.get("with_vehicle")
    line_id = GLOBAL_CONFIG.get("line_id")
    delay_min = GLOBAL_CONFIG.get("delay_min")
    viewing_period = int(GLOBAL_CONFIG.get("viewing_period"))
    et_log_show = GLOBAL_CONFIG.get("et_log_show")
    test_mode = GLOBAL_CONFIG.get("test_mode")
    DB_Setting_mode = GLOBAL_CONFIG.get("DB_Setting_mode")
    KNOXID = GLOBAL_CONFIG.get("KNOXID")
    user_name = GLOBAL_CONFIG.get("user_name")
    email_receiver = GLOBAL_CONFIG.get("email_receiver")

    ROOT = GLOBAL_CONFIG.get("ROOT")
    DB = GLOBAL_CONFIG.get("DB")
    DB_et_daily = GLOBAL_CONFIG.get("DB_et_daily")
    Report = GLOBAL_CONFIG.get("Report")
    low_qual_ppt_save_path = GLOBAL_CONFIG.get("low_qual_ppt_save_path")
    html_save_path = GLOBAL_CONFIG.get("html_save_path")

    et_file_path = GLOBAL_CONFIG.get("et_file_path")
    fab_file_path = GLOBAL_CONFIG.get("fab_file_path")
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
    formatted_datetime = datetime_now.strftime('%y-%m-%d-%H-%M')
    upload_date = datetime_now.strftime('%Y%m%d')

    # ── AI 다단계 해석용: LLM 호출 함수(transport)와 지식베이스 텍스트를 1회 구성 ──
    # 실 환경에서는 gpt_client(OpenAI)를 사용, 로컬에서는 gpt_oss_client.mock_llm 사용.
    # 둘 다 없으면 None → AI 해석 비활성(코드 분석만).
    def _build_llm_fn():
        if GPT_CONNECT and gpt_client is not None:
            def _f(system, user):
                r = gpt_client.chat.completions.create(
                    model="gpt-oss-120b",
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    temperature=0.3)
                return r.choices[0].message.content
            return _f
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
                raw_df = pd.merge(raw_df, real, left_on='item_id', right_on='ITEMID', how='left')
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
                merged_df = Reformatize(merged_df, ALIAS, FORMULA)
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

                            wv_raw_df = pd.merge(wv_raw_df, wv_real, left_on='item_id', right_on='ITEMID', how='left')
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
                            wv_pivot = Reformatize(wv_pivot, wv_ALIAS, wv_FORMULA)
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
                pchk_keep = [c for c in merged_df.columns if 'PCHK' in str(c).upper()]
                # 다중컬럼 ADDP 파생(MA_Window 등: {alias}_minus_margin/_ovl_index 등)도 유지.
                # Reformatize가 {alias}_{subcol} 형태로 만든 컬럼들을 REPORT ORDER alias 접두로 보존한다.
                derived_addp = [c for c in merged_df.columns
                                if c not in columns_to_include_1
                                and any(str(c).startswith(str(a) + "_") for a in columns_to_include_1)]
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

                        not_measured = False

                        target_lot_id = search_key.split('_')[0] #{fab_lot_id}
                        target_root_lot_id = target_lot_id[:5] #{root_lot_id}
                        target_DC_step_id = search_key.split('_')[1] #{DC_step_id}
                        target_DC_step = GLOBAL_CONFIG.get("dc_dict").get(target_DC_step_id) #{DC_step}
                        target_step_merged = target_DC_step + "(" + target_DC_step_id + ")" #{DC_step_id}({DC_step})

                        match_key = target_root_lot_id + "_" + target_DC_step_id #match_key = {root_lot_id}_{DC_step_id}

                        # print('***** fab_lot_id + step_id : ', search_key)
                        # print('***** root_lot_id + step_id : ', match_key)

                        df = merged_df[merged_df['match_key'] == match_key].copy()

                        search_key_rows = df[df['search_key'] == search_key]
                        df['WAFER_ID'] = df['WAFER_ID'].astype(int)
                        empty_cols = search_key_rows.columns[search_key_rows.isna().all()]

                        df.drop(columns=empty_cols, inplace=True)

                        if df.empty :
                            print(f"{search_key}가 비어있습니다.")
                            not_measured = True
                            log_to_file(f"{search_key}에서 HOL DATA가 측정되지 않아 Report 발행되지 않았습니다.", error_log)
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
                        selected_columns = ['WAFER_ID'] + [col for col in df.columns if 'pass' in col]
                        pivot_group = df[selected_columns]
                        pivot_group = pivot_group.groupby('WAFER_ID').mean()*100
                        pivot_group = pivot_group.T
                        VIP_group_raw = pd.merge(pivot_group, reformatter[['REPORT ORDER','PPT_ONLY']], right_index=True, left_index=True, how='right').sort_values('REPORT ORDER').dropna(subset=['REPORT ORDER'])
                        VIP_group = VIP_group_raw.drop('REPORT ORDER',axis=1).dropna(how='all')
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
                        _sb_pass = [c for c in df.columns if 'pass' in c]
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
                        prs_low_qual = insert_score_board(VIP_group_lw, prs_low_qual, target_lot_id, ' / '.join([target_lot_id, target_step_merged]), spec_data=spec_data, config=GLOBAL_CONFIG)

                        # 1-3. BoxPlot 투입 - 메일링 버전 (description dict는 랏 루프 밖에서 1회 파싱)
                        prs_low_qual, metrics_dict = insert_plots(merged_df, prs_low_qual, description_image_info_dict_low_qual, target_lot_id, target_root_lot_id, target_DC_step, target_DC_step_id, spec_data, img_quality = 12, ref=False, reformatter=reformatter, dpi=GLOBAL_CONFIG.ppt_chart_dpi)

                        # 1-3b. 코드 통계 분석(findings) — HTML [0]와 PPT 상세 페이지에 공용 사용
                        code_findings = []
                        try:
                            code_findings = analyze_commonality(
                                merged_df, target_lot_id, metrics_dict, spec_data,
                                main_vehicle=vehicle, config=GLOBAL_CONFIG, reformatter=reformatter,
                                knowledge_text=_ANOMALY_KNOWLEDGE_TEXT)
                            print(f"[INFO] commonality 분석: {len(code_findings)}건 finding")
                        except Exception as ce:
                            print(f"[WARN] commonality 분석 스킵 (오류): {ce}")
                        # Score Board 바로 뒤에 'Anomaly 상세(통계)' 페이지 삽입
                        try:
                            _sb_pages = (len(VIP_group) - 1) // 30 + 1
                            prs_low_qual = insert_findings_page(
                                prs_low_qual, code_findings, after_index=1 + _sb_pages,
                                main_vehicle=vehicle,
                                radius_zones=GLOBAL_CONFIG.get('radius_zones', [60, 100]))
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
                        _pass_cols = [c for c in df.columns if 'pass' in c]
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

                        # 측정값이 전혀 없는 행만 제거(형제 lot 일부 미측정 셀은 회색으로 표기)
                        VIP_group_HTML = VIP_group_HTML.dropna(how='all')

                        # ==================== Score Board HTML 렌더링 (Manual) ====================
                        # Pandas의 to_html()이 만드는 불안정한 멀티인덱스 태그를 방지하기 위해 HTML 태그를 한 땀 한 땀 생성
                        # - 좌측 고정열(LOT_ID/category/Item)은 클래스 기반 sticky (rowspan 사용해도 안깨짐)
                        # - category(CAT2) 연속 동일값은 rowspan으로 병합
                        sb_rows = list(VIP_group_HTML.iterrows())
                        _wcols = list(VIP_group_HTML.columns)

                        # wafer별 WF MAP (≥min_pts 측정 index만) — index 점수행 아래에 'WF MAP 행'으로, wafer 열에 정렬
                        _wf_min = getattr(GLOBAL_CONFIG, 'scoreboard_wfmap_min_pts', 50)
                        _wf_excl = [str(k).upper() for k in getattr(GLOBAL_CONFIG, 'wfmap_exclude_keywords', [])]
                        wfmaps_by_item = {}
                        try:
                            # 항목별 spec/방향만 먼저 추려 배치 렌더링(워커 프로세스 병렬) 요청
                            _wf_specs = []
                            for _it in list(dict.fromkeys([idx[1] for idx, _ in sb_rows])):
                                # 제외 키워드(예: PCHK)가 포함된 item은 pt수와 무관하게 WF MAP 미표시
                                if any(_kw in str(_it).upper() for _kw in _wf_excl):
                                    continue
                                _dir = 'BOTH'
                                _slow = _shigh = None
                                try:
                                    if _it in spec_data.index:
                                        if 'REPORT DIRECTION' in spec_data.columns:
                                            _dv = str(spec_data.loc[_it, 'REPORT DIRECTION']).strip().upper()
                                            if _dv in ('UPPER', 'LOWER', 'BOTH'):
                                                _dir = _dv
                                        if 'SPECLOW' in spec_data.columns:
                                            _v = spec_data.loc[_it, 'SPECLOW']; _slow = None if pd.isna(_v) else _v
                                        if 'SPECHIGH' in spec_data.columns:
                                            _v = spec_data.loc[_it, 'SPECHIGH']; _shigh = None if pd.isna(_v) else _v
                                except Exception:
                                    pass
                                _wf_specs.append({'item': _it, 'direction': _dir,
                                                  'spec_low': _slow, 'spec_high': _shigh})
                            wfmaps_by_item = render_wafer_wfmaps_batch(
                                df, _wf_specs, min_pts=_wf_min, lot_prefix=target_root_lot_id,
                                dpi=getattr(GLOBAL_CONFIG, 'html_wfmap_dpi', 110), by_lot=True)
                            print(f"[INFO] Score Board wafer WF MAP: {len(wfmaps_by_item)}개 index (>={_wf_min}pt)")
                        except Exception as _we:
                            print(f"[WARN] Score Board WF MAP 스킵: {_we}")

                        # 렌더 시퀀스: 각 index 점수행 뒤에 (WF MAP 있으면) 'wfmap' 행 추가
                        render_seq = []   # (kind, cat, item, payload)
                        for idx, row in sb_rows:
                            cat, item = idx
                            render_seq.append(('score', cat, item, row))
                            if item in wfmaps_by_item:
                                render_seq.append(('wfmap', cat, item, wfmaps_by_item[item]))

                        # category 연속 묶음 rowspan (WF MAP 행 포함하여 카운트)
                        seq_cats = [r[1] for r in render_seq]
                        cat_span = {}
                        _j = 0
                        while _j < len(seq_cats):
                            _k = _j
                            while _k + 1 < len(seq_cats) and seq_cats[_k + 1] == seq_cats[_j]:
                                _k += 1
                            cat_span[_j] = _k - _j + 1
                            _j = _k + 1

                        # WF MAP이 있으면 wafer 열 폭을 약간만 넓혀 표시 (48→45px, ~5% 축소 → #25까지 표시)
                        _has_wf = len(wfmaps_by_item) > 0
                        _wf_w = 45
                        # 메일 클라이언트는 <style> CSS를 무시하므로 각 셀에 inline style로 직접 지정
                        _SB_BD = 'border:1px solid #2c2c2c;'      # 셀 구분선(inline)
                        _sb_waf_w = _wf_w if _has_wf else 40      # wafer 셀 폭(숫자 잘림 방지) inline min-width
                        _SB_WAF = f'{_SB_BD} text-align:center; width:{_sb_waf_w}px; min-width:{_sb_waf_w}px; max-width:{_sb_waf_w}px;'
                        _SB_CAT = f'{_SB_BD} text-align:center; min-width:77px;'      # category 고정열
                        _SB_ITEM = f'{_SB_BD} text-align:center; min-width:240px;'    # Item 고정열
                        sb_html = ''
                        if _has_wf:
                            sb_html += (f'<style>.score-board td.sb-val, .score-board th.sb-waf'
                                        f'{{width:{_wf_w}px !important; min-width:{_wf_w}px !important; '
                                        f'max-width:{_wf_w}px !important;}}</style>\n')
                        # lot 그룹(헤더 colspan용): _wcols 순서대로 같은 lot을 묶음
                        _lot_groups = []   # [(lot, [col, ...]), ...]
                        for _c in _wcols:
                            if _lot_groups and _lot_groups[-1][0] == _c[0]:
                                _lot_groups[-1][1].append(_c)
                            else:
                                _lot_groups.append((_c[0], [_c]))

                        sb_html += '<table class="score-board" style="border-collapse:collapse;">\n  <thead>\n'
                        sb_html += '    <tr>\n'
                        sb_html += f'      <th colspan="2" class="sb-frozen-lot" style="{_SB_BD} text-align:center; background-color:#d9e1f2;">LOT_ID</th>\n'
                        # root_lot_id가 같은 형제 lot을 각각 헤더로 분리 (target lot은 강조)
                        for _lot, _cols in _lot_groups:
                            _is_tgt = (str(_lot) == str(target_lot_id))
                            _bg = '#dbe7c8' if _is_tgt else '#f0f0f0'
                            _fw = 'bold' if _is_tgt else 'normal'
                            sb_html += (f'      <th colspan="{len(_cols)}" style="{_SB_BD} text-align:center; '
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
                            if kind == 'score':
                                row = payload
                                sb_html += (f'      <td class="sb-item row_heading" style="{_SB_ITEM} font-weight:bold; '
                                            f'background-color:#ebf4ff;">{item}</td>\n')
                                for col in _wcols:
                                    val = row[col]
                                    if pd.isna(val) or val == "":
                                        sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:{GLOBAL_CONFIG.score_color_na};"></td>\n'
                                    else:
                                        # 연속 색상(PPT와 동일), ITEM별 스케일 override 지원
                                        bg_color, color = GLOBAL_CONFIG.score_color(val, item)
                                        sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:{bg_color}; color:{color}; font-weight:bold;">{val:.1f}</td>\n'
                            else:  # 'wfmap' 행 — wafer 열에 각 wafer의 WF MAP 정렬
                                maps = payload
                                sb_html += (f'      <td class="sb-item row_heading" style="{_SB_ITEM} font-size:9px; color:#666; '
                                            'background-color:#ebf4ff;">WF MAP</td>\n')
                                for col in _wcols:
                                    _b = maps.get(f"{col[0]}|{col[1]}")
                                    if _b:
                                        sb_html += (f'      <td class="sb-val" style="{_SB_WAF} background-color:#ffffff; padding:0;">'
                                                    f'<img src="data:image/png;base64,{_b}" '
                                                    f'style="width:{_wf_w - 2}px; height:{_wf_w - 2}px; display:block; margin:auto;"/></td>\n')
                                    else:
                                        sb_html += f'      <td class="sb-val" style="{_SB_WAF} background-color:#f4f4f4;"></td>\n'
                            sb_html += '    </tr>\n'
                        sb_html += '  </tbody>\n</table>\n'
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

                        # 메일 클라이언트용 inline style (셀 구분선 + 가운데 정렬)
                        _IT_BD = 'border:1px solid #2c2c2c;'
                        _IT_CTR = 'text-align:center !important;'   # 헤더 CSS(left) override
                        _IT_WAF = 'width:56px; min-width:56px; max-width:56px;'

                        it_html = '<table class="inline-table" style="border-collapse:collapse;">\n'
                        it_html += '  <thead>\n'
                        it_html += '    <tr>\n'
                        for col in inlinedata_filtered_pivot.columns:
                            if col in _head_cols:
                                it_html += f'      <th class="row_heading" style="{_IT_BD} {_IT_CTR} background-color:#e2efda !important;">{col}</th>\n'
                            elif col in ['UCL', 'CL', 'LCL']:
                                it_html += f'      <th style="{_IT_BD} {_IT_CTR} background-color:#f0f0f0 !important;">{col}</th>\n'
                            else:
                                col_str = str(col) if str(col).startswith('#') else '#' + str(col)
                                it_html += f'      <th style="{_IT_BD} {_IT_CTR} {_IT_WAF} background-color:#f0f0f0 !important;">{col_str}</th>\n'
                        it_html += '    </tr>\n'
                        it_html += '  </thead>\n'
                        it_html += '  <tbody>\n'
                        for _ri, (_, row) in enumerate(inlinedata_filtered_pivot.iterrows()):
                            it_html += '    <tr>\n'
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

                                if col in ['UCL', 'CL', 'LCL']:
                                    style = f'{_IT_BD} {_IT_CTR} background-color:#e0f7fa;'
                                elif col in _head_cols:
                                    style = f'{_IT_BD} {_IT_CTR} vertical-align:middle; background-color:#f0fff4;'
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
                        lot_detail_html = '<table class="lot-detail-table" style="border-collapse:collapse;">\n  <thead>\n    <tr>\n'
                        for _c in et_log.columns:
                            lot_detail_html += f'      <th style="{_LD_CELL} background-color:#e8edf3; font-weight:bold;">{_ld_esc(_c)}</th>\n'
                        lot_detail_html += '    </tr>\n  </thead>\n  <tbody>\n'
                        for _, _r in et_log.iterrows():
                            lot_detail_html += '    <tr>\n'
                            for _c in et_log.columns:
                                _v = _r[_c]
                                _vs = '' if pd.isna(_v) else _ld_esc(_v)
                                lot_detail_html += f'      <td style="{_LD_CELL}">{_vs}</td>\n'
                            lot_detail_html += '    </tr>\n'
                        lot_detail_html += '  </tbody>\n</table>\n'

                        # ==================== [0] Anomaly: 코드 분석 + (선택)AI 다단계 해석 + Trend chart ====================
                        # 코드(analyze_commonality)는 AI 유무와 무관하게 항상 동작하여 통계 Finding을 산출.
                        # use_gpt_summary가 켜져 있고 LLM이 가능하면, 그 Finding을 입력으로 AI 다단계 해석을 곁들임.
                        _top_n = getattr(GLOBAL_CONFIG, 'anomaly_trend_chart_top_n', 6)

                        # 1) 코드 통계 분석 결과(위 1-3b에서 계산) → HTML 요약(상위 5건 + PPT 상세 참조)
                        code_summary_html = ""
                        try:
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
                        top_item_names = []
                        _seen = set()
                        for _f in (code_findings or []):
                            for _it in str(_f.get('item', '')).split(','):
                                _it = _it.strip()
                                if (not _it) or (_it in _seen) or (_it not in merged_df.columns) or (not _has_png(_it)):
                                    continue
                                top_item_names.append(_it); _seen.add(_it)
                                if len(top_item_names) >= _top_n: break
                            if len(top_item_names) >= _top_n: break
                        if len(top_item_names) < _top_n and metrics_dict:
                            _sigma = getattr(GLOBAL_CONFIG, 'anomaly_deviation_sigma', 1.5)
                            _excl_items = list(getattr(GLOBAL_CONFIG, 'anomaly_exclude_items', []) or [])
                            # WF MAP 제외 키워드 항목도 anomaly 판정 대상에서 제외(부분일치 → *KEYWORD*)
                            _excl_items += [f"*{str(_k).strip()}*"
                                            for _k in (getattr(GLOBAL_CONFIG, 'wfmap_exclude_keywords', []) or [])
                                            if str(_k).strip()]
                            _anom = [m for m in metrics_dict.values()
                                     if m.get('spec_out_count', 0) > 0 or m.get('deviation', 0.0) > _sigma]
                            _anom.sort(key=lambda m: (m.get('spec_out_count', 0), m.get('deviation', 0.0)), reverse=True)
                            for m in _anom:
                                _it = m['item']
                                if (_it in _seen) or (_it not in merged_df.columns) or (not _has_png(_it)) \
                                        or item_excluded(_it, _excl_items):
                                    continue
                                top_item_names.append(_it); _seen.add(_it)
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

                                    def _trend_block(item, is_spec, img_b64):
                                        if is_spec:
                                            _stat, _bg, _fg = 'SPEC OUT', '#d32f2f', '#ffffff'
                                        else:
                                            _stat, _bg, _fg = 'WARNING', '#f9a825', '#1a1a1a'
                                        _bstyle = ('font-size:10px; font-weight:bold; padding:2px 7px; '
                                                   'border-radius:3px; box-shadow:0 1px 2px rgba(0,0,0,.35);')
                                        _sticker = f'<span style="background:{_bg}; color:{_fg}; {_bstyle}">{_stat}</span>'
                                        return (
                                            '<div style="position:relative; flex:0 0 380px;">'
                                            f'<div style="position:absolute; top:6px; left:6px; z-index:2;">{_sticker}</div>'
                                            f'<img src="data:image/png;base64,{img_b64}" style="display:block; width:100%; border:1px solid #ddd;"/>'
                                            '</div>')

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

                                    _spec_rows, _warn_blocks = [], []
                                    for item in top_item_names:
                                        safe_item = re.sub(r'[\\/:*?"<>|]', '_', str(item))
                                        img_path = f"RUN/TEMP/{safe_item}.png"
                                        if not os.path.exists(img_path):
                                            continue
                                        with open(img_path, "rb") as f:
                                            img_b64 = base64.b64encode(f.read()).decode('utf-8')
                                        _is_spec = metrics_dict.get(item, {}).get('spec_out_count', 0) > 0
                                        if not _is_spec:
                                            _warn_blocks.append(_trend_block(item, False, img_b64))
                                            continue
                                        # 이상(SPEC OUT) — 우측에 spec-out WF MAP 최대한 많이
                                        _wf_block = ''
                                        if _wf_on:
                                            try:
                                                _slow, _shigh = _spec_bounds(item)
                                                _wfmaps = render_specout_wfmaps_b64(
                                                    merged_df, item, spec_low=_slow, spec_high=_shigh,
                                                    target_lot=target_lot_id, max_maps=_wf_max)
                                                if _wfmaps:
                                                    _cells = ''
                                                    for _wf in _wfmaps:
                                                        # (label, b64, is_target) — 하위호환: 2-튜플이면 target 아님
                                                        _lab, _b = _wf[0], _wf[1]
                                                        _is_tgt = _wf[2] if len(_wf) > 2 else False
                                                        # target lot WF MAP 라벨은 진한 파란색 + 볼드로 강조
                                                        _lab_style = ('font-size:8px; white-space:nowrap; '
                                                                      + ('color:#0033cc; font-weight:bold;' if _is_tgt else 'color:#555;'))
                                                        _cells += (
                                                            '<div style="text-align:center;">'
                                                            f'<img src="data:image/png;base64,{_b}" '
                                                            'style="width:58px; height:58px; display:block; margin:0 auto; border:1px solid #1f4e79;"/>'
                                                            f'<div style="{_lab_style}">{_lab}</div>'
                                                            '</div>')
                                                    _wf_block = (
                                                        '<div style="flex:1; display:grid; grid-template-columns: repeat(auto-fill, 64px); '
                                                        f'gap:4px; align-content:start;">{_cells}</div>')
                                            except Exception as _we:
                                                print(f"[WARN] spec-out WF MAP 스킵 ({item}): {_we}")
                                        _spec_rows.append(
                                            '<div style="display:flex; align-items:flex-start; gap:12px; margin-bottom:8px;">'
                                            f'{_trend_block(item, True, img_b64)}{_wf_block}</div>')

                                    # '이상'/'주의' 탭 라벨은 표시하지 않는다. 각 차트 좌상단의
                                    # SPEC OUT / WARNING 스티커가 상태 식별 역할을 대신한다.
                                    _parts = []
                                    if _spec_rows:
                                        _parts.extend(_spec_rows)
                                    if _warn_blocks:
                                        _parts.append(
                                            '<div style="display:flex; flex-wrap:wrap; align-items:flex-start; gap:8px;">'
                                            f'{"".join(_warn_blocks)}</div>')
                                    anomaly_html = ''.join(_parts) if _parts else '<p>이상항목 없음</p>'
                                else:
                                    anomaly_html = '<p>이상항목 없음</p>'
                            except Exception as ae:
                                print(f"[WARN] 이상 Trend chart 생성 스킵 (오류): {ae}")
                        else:
                            print("[INFO] show_anomaly_trend_chart=False → 이상 Trend chart 스킵")

                        # 4) (선택) AI 다단계 해석 — code_findings를 입력으로. 실패/미사용 시 None → 코드 분석만 표시
                        ai_html = None
                        if (GLOBAL_CONFIG.use_gpt_summary and getattr(GLOBAL_CONFIG, 'use_gpt_multistep', True)
                                and code_findings and _LLM_FN is not None):
                            try:
                                ai_html = interpret_with_ai(
                                    code_findings, metrics_dict, _ANOMALY_KNOWLEDGE_TEXT,
                                    _LLM_FN, config=GLOBAL_CONFIG, target_lot_id=target_lot_id)
                                print("[INFO] AI 다단계 해석 적용" if ai_html else "[INFO] AI 다단계 해석 결과 없음")
                            except Exception as ae:
                                print(f"[WARN] AI 다단계 해석 스킵 (오류): {ae}")
                        elif not GLOBAL_CONFIG.use_gpt_summary:
                            print("[INFO] use_gpt_summary=False → AI 해석 스킵 (코드 분석만)")

                        # ==================== HTML 조립 ====================
                        sub_title = f'{target_lot_id} / {target_step_merged}'
                        html_content = html_code.replace('sub_title', sub_title)

                        # [0] 섹션 = (AI 다단계 해석 있으면 상단) + 코드 자동 분석(통계 Finding) + Trend chart 그리드
                        _ai_block = (ai_html + '<hr style="border:none;border-top:1px solid #eee;margin:8px 0;">') if ai_html else ''
                        _chart_sub = ('<div class="section-title" style="font-size:13px; margin-top:14px;">'
                                      'Anomaly Trend Chart</div>')
                        html_content = html_content.replace(
                            '<div id="target0"></div>',
                            f'<div id="target0"><div class="section-title">■ [0] Anomaly Summary</div>'
                            f'{_ai_block}{code_summary_html}{_chart_sub}{anomaly_html}</div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target1"></div>',
                            f'<div id="target1"><div class="section-title">■ [1] Score Board</div>'
                            f'<div style="font-size:12px; color:#555; margin:2px 0 6px 2px;">'
                            f'※ {_wf_min}pt 이상 측정된 이력이 있는 아이템은 각 wafer 아래에 WF MAP이 함께 표시됩니다.</div>'
                            # Score Board: 컨테이너 스크롤 없이 전체 항목을 한번에 펼침(max-height 없음, overflow visible).
                            # → thead(LOT_ID/wafer 헤더)가 페이지 스크롤 시 상단에 sticky 고정됨(score-board-open 클래스).
                            f'<div class="table-container score-board-open">{score_board_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target2"></div>',
                            f'<div id="target2"><div class="section-title">■ [2] Inline Table</div>'
                            f'<div class="table-container">{inline_table_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target3"></div>',
                            f'<div id="target3"><div class="section-title">■ [3] 최근 DC측정자재 상세</div>'
                            f'<div class="table-container">{lot_detail_html}</div></div>'
                        )
                        html_content = html_content.replace(
                            '<div id="target4"></div>',
                            ''
                        )

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
                                except Exception as _de:
                                    pass
                                client.upload_file(_s3_local, bucket_dx, s3_key)
                                print_status("S3 업로드", "ok", f"{bucket_dx}/{s3_key}")
                            except Exception as s3e:
                                print_status("S3 업로드", "fail", f"{search_key}: {s3e}")
                        else:
                            print_status("S3 업로드", "off", f"{search_key} → S3 미연결 스킵")

                        # ==================== 사내 메일 API 발송 (PPT + HTML) ====================
                        # My_config.use_email_send 로 on/off. 생성된 HTML(html_content)은 본문으로,
                        # 저화질 PPT는 첨부로 전송한다.
                        if getattr(GLOBAL_CONFIG, 'use_email_send', False):
                            _mail_fh = None
                            try:
                                html_code_final = html_content   # 생성된 HTML 코드 문자열
                                # 수신 그룹(=메일링 xlsx의 시트명). config email_receiver가 리스트면 첫 항목 사용.
                                email_receiver_now = (email_receiver[0]
                                                      if isinstance(email_receiver, (list, tuple)) and email_receiver
                                                      else email_receiver)
                                title = f'[HOL] {vehicle} {target_lot_id} {target_step_merged} HOL AUTO REPORT'

                                email_list = get_email_list(email_list_path, email_receiver_now)

                                payload_content = {
                                    "content": f'{html_code_final}',
                                    "receiverList": email_list,
                                    "senderMailAddress": f"{KNOXID}@samsung.com",
                                    "statusCode": "SENT",
                                    "title": f'{title}',
                                }
                                payload = {'mailSendString': f'{payload_content}'}

                                _ppt_full = os.path.join(low_qual_ppt_save_path, final_ppt_file_name_DX)
                                _mail_fh = open(_ppt_full, 'rb')
                                files = [
                                    ('file', (final_ppt_file_name_DX, _mail_fh, 'application/vnd.ms-powerpoint'))
                                ]
                                headers = {'x-dep-ticket': GLOBAL_CONFIG.get("TICKET")}

                                response = requests.request(
                                    "POST", GLOBAL_CONFIG.get("url"),
                                    headers=headers, data=payload, files=files)
                                _sc = getattr(response, 'status_code', None)
                                if _sc == 200:
                                    print_status("메일 발송", "ok",
                                                 f"{target_lot_id}_{target_DC_step} 완료 (수신 {len(email_list)}명, HTTP {_sc})")
                                else:
                                    # 200이 아니면 상세 에러 내용을 터미널에 출력
                                    try:
                                        _body = response.text
                                    except Exception:
                                        _body = '(응답 본문 읽기 실패)'
                                    print_status("메일 발송", "fail",
                                                 f"{target_lot_id}_{target_DC_step} — HTTP {_sc}")
                                    print(f"[ERROR] 메일 발송 응답 오류 (HTTP {_sc}) 상세: {_body}")
                            except Exception as _me:
                                print_status("메일 발송", "fail", f"{target_lot_id}_{target_DC_step}: {_me}")
                            finally:
                                try:
                                    if _mail_fh is not None:
                                        _mail_fh.close()
                                except Exception:
                                    pass
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
        shutdown_chart_pool()   # 병렬 렌더링 워커 풀 정리 (atexit에도 등록되어 있으나 명시 종료)
        print(f'[INFO] ============== {vehicle} 전체 프로세스 완료 ==============')

    else:
        print("[ERROR] reformatter 검증 실패. 프로그램 종료.")


if __name__ == "__main__":
    main()
