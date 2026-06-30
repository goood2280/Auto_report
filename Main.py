# ----- Python 표준 라이브러리
import gc
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta, date
import warnings

# ----- 서드파티
try:
    from gpt_oss_client import generate_report_summary
except ImportError:
    # gpt_oss_client는 사내 전용 모듈. 없는 환경(로컬/오프라인)에서는
    # GPT 요약을 건너뛰도록 빈 결과를 반환하는 폴백을 사용합니다.
    def generate_report_summary(metrics_dict):
        print("[WARN] gpt_oss_client 미설치 - GPT 요약 비활성화 (빈 요약 반환)")
        return "", []
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
from bigdataquery import *
from My_Function import *
from My_config import GLOBAL_CONFIG
from anomaly_engine import run_anomaly_pipeline

# ==================================================================================================================================
# GPT OSS 120B API 연결 설정
# ==================================================================================================================================

from openai import OpenAI

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
        print("[INFO] GPT OSS 120B API 연결 성공")
    except Exception as e:
        print(f"[WARN] GPT OSS 120B API 연결 실패: {e}")
        gpt_client = None
else:
    print("[WARN] GPT_API_BASE_URL 또는 GPT_CREDENTIAL_KEY 가 .env 에 설정되지 않았습니다.")

# ==================================================================================================================================

warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ==================================================================================================================================

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
template_ppt_path = GLOBAL_CONFIG.get("template_ppt_path")
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

for target_path in [ROOT, DB, DB_et_daily, log, Report, low_qual_ppt_save_path, html_save_path]:
    if not os.path.exists(target_path):
        os.makedirs(target_path)

import builtins
import atexit

_original_print = builtins.print
_run_log_buffer = []

def _run_log_print(*args, **kwargs):
    _original_print(*args, **kwargs)
    try:
        if 'loop_log' in globals() and loop_log:
            msg = " ".join(str(a) for a in args)
            _run_log_buffer.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def _flush_logs_to_top():
    if not ('loop_log' in globals() and loop_log) or not _run_log_buffer:
        return
    
    new_logs = "".join(_run_log_buffer)
    existing_logs = ""
    
    if os.path.exists(loop_log):
        try:
            with open(loop_log, "r", encoding="utf-8") as f:
                existing_logs = f.read()
        except Exception:
            pass

    combined_logs = new_logs + existing_logs
    
    # 50MB Limit (Approx 50,000,000 chars)
    max_chars = 50 * 1024 * 1024
    if len(combined_logs) > max_chars:
        combined_logs = combined_logs[:max_chars]
        last_nl = combined_logs.rfind('\n')
        if last_nl != -1:
            combined_logs = combined_logs[:last_nl+1]
            
    try:
        with open(loop_log, "w", encoding="utf-8") as f:
            f.write(combined_logs)
    except Exception:
        pass

atexit.register(_flush_logs_to_top)
builtins.print = _run_log_print

# =============================================== Main Loop 실행 ====================================================================
bucket_dx = GLOBAL_CONFIG.get("bucket_dx") 
bucket_simyung = GLOBAL_CONFIG.get("bucket_simyung") 

# S3 client (사내 환경 전용 - 로컬에서는 graceful skip)
S3_CONNECT = False
client = None
try:
    client = boto3.client(
                service_name='s3', region_name='DS',
                aws_access_key_id=GLOBAL_CONFIG.get("s3_aws_access_key_id"),  
                aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),
                endpoint_url=GLOBAL_CONFIG.get("endpoint_url"))
    S3_CONNECT = True
except Exception as s3_init_err:
    print(f"[WARN] S3 client 초기화 실패 (로컬 테스트 모드): {s3_init_err}")

datetime_now = datetime.now()
formatted_datetime = datetime_now.strftime('%y-%m-%d-%H-%M')
upload_date = datetime_now.strftime('%Y%m%d')

reformatter = pd.read_csv(f'reformatter/{vehicle}_reformatter.csv') 

reformatter_check = reformatter_verify(reformatter)
# print("reformatter_check : ", reformatter_check)

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
    final_lot_log['dc_done'] = final_lot_log.apply(lambda row: True \
        if combined_lot_log[combined_lot_log['prime_key'] == row['prime_key']]['dc_done'].any() else row['dc_done'],\
        axis=1
    )

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
            addp['addpscale'] = addp['SCALE FACTOR'].astype(str) + '*(' + addp['ADDP FORM'] + ')'
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
            
            # ── Scale Factor 적용 ──
            raw_df = pd.merge(raw_df, real, left_on='item_id', right_on='ITEMID', how='left')
            raw_df['et_value'] = raw_df['et_value'].astype(float)
            raw_df.loc[raw_df['SCALE FACTOR'].notna(), 'et_value'] *= raw_df.loc[raw_df['SCALE FACTOR'].notna(), 'SCALE FACTOR'].astype(float)
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
                        wv_addp['addpscale'] = wv_addp['SCALE FACTOR'].astype(str) + '*(' + wv_addp['ADDP FORM'] + ')'
                        wv_ALIAS = list(map(str, wv_addp.ALIAS))
                        wv_FORMULA = list(map(str, wv_addp.addpscale))
                        
                        wv_raw_df = pd.merge(wv_raw_df, wv_real, left_on='item_id', right_on='ITEMID', how='left')
                        wv_raw_df['et_value'] = wv_raw_df['et_value'].astype(float)
                        wv_raw_df.loc[wv_raw_df['SCALE FACTOR'].notna(), 'et_value'] *= wv_raw_df.loc[wv_raw_df['SCALE FACTOR'].notna(), 'SCALE FACTOR'].astype(float)
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
            columns_to_include = columns_to_include_1 +  columns_to_include_2
            filtered_columns = [col for col in columns_to_include if col in merged_df.columns]
            merged_df = merged_df[filtered_columns]

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
            
            # Ref WF 정보
            #YLD 정보 Download
            if vehicle == 'Solomon1' or vehicle == 'Solomon2' :
                client.download_file(bucket_simyung, '/SF3_Product/Solomon_EVT1/Result/SF3_SOL_EVT1_Result_Data.csv', 'SF3_SOL_EVT1_Result_Data.csv') 
                reference_df = pd.read_csv('SF3_SOL_EVT1_Result_Data.csv')
                reference_df['WAFER_ID'] = reference_df['WAFER_ID'].astype(int)
                reference_df['END_TIME'] = pd.to_datetime(reference_df['END_TIME'])
                reference_df = reference_df.loc[reference_df.groupby(['ROOT_LOT_ID', 'WAFER_ID'])['END_TIME'].idxmax()]
                reference_df = reference_df[reference_df['PGM_VER']>12][['ROOT_LOT_ID','WAFER_ID','END_TIME','LOT_ID','PGM_VER','YLD','L1/L2','SCAN_LH','SRAM_LH']]
                reference_df = reference_df.sort_values(by=['YLD'], ascending=False).reset_index()
                reference_df = reference_df[['ROOT_LOT_ID','WAFER_ID','END_TIME','LOT_ID','PGM_VER','YLD','L1/L2','SCAN_LH','SRAM_LH']].reset_index()
            
                for i in range(0, 100):
                    try :
                        reference_lot_id = reference_df['ROOT_LOT_ID'].iloc[i]
                        reference_wafer_id = reference_df['WAFER_ID'].iloc[i]
                        reference_yld = reference_df['YLD'].iloc[i]
                        if not merged_df[(merged_df['ROOT_LOT_ID'] == reference_lot_id) & (merged_df['WAFER_ID'] == reference_wafer_id) & (merged_df['DC_Split'] == "MFDC")].empty :
                            break
                    except : 
                        pass
            else :
                try:
                    reference_df = pd.read_csv('Thetis1_YLD.csv')
                    reference_df['WAFER_ID'] = reference_df['WAFER_ID'].astype(int)
                    reference_df['TKOUT_TIME'] = pd.to_datetime(reference_df['TKOUT_TIME'])
                    reference_df = reference_df.loc[reference_df.groupby(['ROOT_LOT_ID', 'WAFER_ID'])['TKOUT_TIME'].idxmax()]
                    reference_df = reference_df.sort_values(by=['YLD'], ascending=False).reset_index()

                    for i in range(0, 2500):
                        try :
                            reference_lot_id = reference_df['ROOT_LOT_ID'].iloc[i]
                            reference_wafer_id = reference_df['WAFER_ID'].iloc[i]
                            reference_yld = reference_df['YLD'].iloc[i]
                            if not merged_df[(merged_df['ROOT_LOT_ID'] == reference_lot_id) & (merged_df['WAFER_ID'] == reference_wafer_id) & (merged_df['DC_Split'] == "MFDC")].empty :
                                break
                        except : 
                            pass
                except FileNotFoundError:
                    print("[WARN] Thetis1_YLD.csv 파일이 없습니다. reference 데이터를 None으로 설정합니다.")
                    reference_lot_id = None
                    reference_wafer_id = None
                    reference_yld = None

            if reference_yld is not None and 'reference_df' in locals():
                reference_df = reference_df[reference_df['YLD'] == reference_yld].reset_index()
                
            html_code = GLOBAL_CONFIG.get("html_code")

            for search_key in search_strings : #search key = match key, fablot_id + dc_step_id
                try : 
                    print(f'[INFO] *****{search_key}에 대한 AUTO LOT Report 발행 시작')
                    
                    not_measured = False
                    
                    target_lot_id = search_key.split('_')[0] #{fab_lot_id}
                    target_root_lot_id = target_lot_id[:5] #{root_lot_id}
                    target_DC_step_id = search_key.split('_')[1] #{DC_step_id}
                    target_DC_step = GLOBAL_CONFIG.get("dc_dict").get(target_DC_step_id) #{DC_step}
                    target_step_merged = target_DC_step + "(" + target_DC_step_id + ")" #{DC_step_id}({DC_step})

                    match_key = target_root_lot_id + "_" + target_DC_step_id #match_key = {root_lot_id}_{DC_step_id}

                    # print('***** fab_lot_id + step_id : ', search_key)
                    # print('***** root_lot_id + step_id : ', match_key)

                    df = merged_df[(merged_df['match_key'] == match_key) | ((merged_df['ROOT_LOT_ID'] == reference_lot_id) & (merged_df['WAFER_ID'] == reference_wafer_id) & (merged_df['DC_Split'] == "MFDC"))].copy()

                    search_key_rows = df[df['search_key'] == search_key]
                    df['WAFER_ID'] = df['WAFER_ID'].astype(int)
                    empty_cols = search_key_rows.columns[search_key_rows.isna().all()]
                    
                    df.drop(columns=empty_cols, inplace=True)
                    
                    if df.empty :
                        print(f"{search_key}가 비어있습니다.")
                        not_measured = True
                        log_to_file(f"{search_key}에서 HOL DATA가 측정되지 않아 Report 발행되지 않았습니다.", error_log)
                        continue

                    df.loc[(df['ROOT_LOT_ID'] == reference_lot_id) & (df['WAFER_ID'] == reference_wafer_id) , 'WAFER_ID'] = 0

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
                    VIP_group_raw = VIP_group_raw[(VIP_group_raw['PPT_ONLY'] != 1.0)]
                    VIP_group_raw = VIP_group_raw.drop('PPT_ONLY', axis=1)

                    # VIP_group 생성 *presentation 생성용 dataframe
                    VIP_group.index = VIP_group.index.str.replace('pass_rate_', '') 

                    # VIP_group_HTML 생성 *VIP_group copy (HTML 카테고리 구분자는 CAT1 기준)
                    VIP_group_HTML = pd.merge(VIP_group_raw,reformatter[['CAT1','REPORT ORDER']].dropna(subset=['REPORT ORDER']).drop('REPORT ORDER',axis=1)\
                                            ,right_index=True, left_index=True, how='left').reset_index()
                    VIP_group_HTML = VIP_group_HTML.rename(columns={'CAT1': 'CATEGORY', 'index': 'ITEM_ID', 'pass_rate': 'ITEM_ID'})
                    VIP_group_HTML['ITEM_ID'] = VIP_group_HTML['ITEM_ID'].str.replace('pass_rate_', '')
                    VIP_group_HTML = VIP_group_HTML.set_index(['CATEGORY', 'ITEM_ID'])
                    VIP_group_HTML = VIP_group_HTML.drop('REPORT ORDER',axis=1)
                    VIP_group_HTML = VIP_group_HTML.dropna(how='all')

                    # ========================================= PPT file name 생성 ==========================================

                    rname = f'HOL_{target_DC_step}_Report_v13'
                    fname = f'{upload_date}-{prod}-{target_root_lot_id}-{rname}.html' #html 저장이름
                    final_ppt_file_name_DX = f'{upload_date}-{prod}-{target_root_lot_id}-{rname}.pptx' #pptx 저장이름, DX System 및 S3 DB 저장

                    # ========================================= 저화질 버전 ppt 제작 =========================================

                    clear_temp_inside_run()
                    clear_anomaly_inside_run()
                    
                    # 1-1. Title page 투입
                    print(f'[INFO]..{vehicle}_{target_lot_id}_{target_step_merged}_HOL_AUTO_REPORT 저화질 버전 제작 시작..\n')
                    prs_low_qual = make_title_page(template_ppt_path, vehicle, target_lot_id, target_step_merged)

                    # 1-2. Scoreboard 투입
                    prs_low_qual = insert_score_board(VIP_group, prs_low_qual, target_lot_id, ' / '.join([target_lot_id, target_step_merged]), spec_data=spec_data, config=GLOBAL_CONFIG)

                    # 1-3. BoxPlot 투입 - 메일링 버전
                    description_image_info_dict_low_qual = calcaulate_description_image_info_dict(description_ppt_path, img_quality = 20)
                    prs_low_qual, metrics_dict = insert_plots(merged_df, prs_low_qual, description_image_info_dict_low_qual, target_lot_id, target_root_lot_id, target_DC_step, target_DC_step_id, spec_data, img_quality = 12, ref=False, reformatter=reformatter, dpi=GLOBAL_CONFIG.ppt_chart_dpi)
                
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
                    
                    # [PATCH] Inline Table 멀티 인덱스 복구
                    inlinedata_filtered_pivot['ITEMNAME'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict_ITEMNAME)
                    inlinedata_filtered_pivot['ITEM_ID'] = inlinedata_filtered_pivot.index.map(inline_grouped_dict_ITEM_ID)
                    inlinedata_filtered_pivot = inlinedata_filtered_pivot.set_index(['ITEMNAME', 'ITEM_ID'])
                    inlinedata_filtered_pivot.index.names = ['Module', 'Item']
                    
                    
                    # HTML 생성부분 - Mail body
                    VIP_group_HTML.columns = ['#' + str(col) for col in VIP_group_HTML.columns]

                    column_mapping = {wafer_id : (fab_lot_id, wafer_id) for fab_lot_id, wafer_id in wf_matching_list}
                    # print('column_mapping :', column_mapping)

                    # 첫번째 값이 동일한 항목들을 뭉치기
                    grouped_values = {}
                    for key, value in column_mapping.items():
                        first_value = value[0]
                        if first_value not in grouped_values:
                            grouped_values[first_value] = []
                        grouped_values[first_value].append(key)
                    print("grouped_values : ",grouped_values)
                    
                    sorted_keys = []
                    for key_list in grouped_values.values():
                        sorted_keys.extend(sorted(key_list, key=lambda x: int(x[1:])))
                    if '#0' in sorted_keys:
                        sorted_keys.insert(0, sorted_keys.pop(sorted_keys.index('#0')))
                    print("sorted_keys : ",sorted_keys)

                    VIP_group_HTML = VIP_group_HTML[sorted_keys]
                    VIP_group_HTML.index.names = ['category', 'Item']

                    # "우측에 빈칸있는 행 안나오게해줘" -> 하나라도 비어있는 데이터 drop
                    VIP_group_HTML = VIP_group_HTML.dropna(how='any')

                    # 컬럼 단일 레벨화 (WAFER_ID만 남김)
                    VIP_group_HTML.columns = [col for col in VIP_group_HTML.columns]
                    VIP_group_HTML.columns.name = ' ' # 2줄 헤더 유도를 위한 빈칸 이름 설정

                    # ==================== Score Board HTML 렌더링 (Manual) ====================
                    # Pandas의 to_html()이 만드는 불안정한 멀티인덱스 태그를 방지하기 위해 HTML 태그를 한 땀 한 땀 생성
                    sb_html = '<table class="score-board">\n'
                    sb_html += '  <thead>\n'
                    # 헤더 첫번째 줄
                    sb_html += '    <tr>\n'
                    sb_html += f'      <th colspan="2" class="row_heading" style="text-align:center; background-color:#d9e1f2;">LOT_ID</th>\n'
                    sb_html += f'      <th colspan="{len(VIP_group_HTML.columns)}" style="text-align:center; background-color:#f0f0f0;">{target_lot_id}</th>\n'
                    sb_html += '    </tr>\n'
                    # 헤더 두번째 줄
                    sb_html += '    <tr>\n'
                    sb_html += '      <th style="background-color:#d9e1f2;">category</th>\n'
                    sb_html += '      <th style="background-color:#d9e1f2;">Item</th>\n'
                    for col in VIP_group_HTML.columns:
                        sb_html += f'      <th style="background-color:#f0f0f0; width:70px; min-width:70px; max-width:70px;">{col}</th>\n'
                    sb_html += '    </tr>\n'
                    sb_html += '  </thead>\n'
                    sb_html += '  <tbody>\n'
                    
                    for idx, row in VIP_group_HTML.iterrows():
                        cat, item = idx
                        sb_html += '    <tr>\n'
                        sb_html += f'      <td class="row_heading" style="font-weight:bold; background-color:#ebf4ff;">{cat}</td>\n'
                        sb_html += f'      <td class="row_heading" style="font-weight:bold; background-color:#ebf4ff;">{item}</td>\n'
                        for col in VIP_group_HTML.columns:
                            val = row[col]
                            if pd.isna(val) or val == "":
                                sb_html += '      <td style="background-color:#555555;"></td>\n'
                            else:
                                # Score Board 색상 임계값 (config에서 로드)
                                _thresholds = GLOBAL_CONFIG.score_thresholds  # [100.0, 90.0, 70.0, 50.0]
                                _colors = GLOBAL_CONFIG.score_colors
                                bg_color = '#ffffff'
                                color = '#000000'
                                if val == _thresholds[0]:       # == 100
                                    bg_color = _colors[100]['bg']
                                    color = _colors[100]['fg']
                                elif val >= _thresholds[1]:     # >= 90
                                    bg_color = _colors[90]['bg']
                                    color = _colors[90].get('fg', '#000000')
                                elif val >= _thresholds[2]:     # >= 70
                                    bg_color = _colors[70]['bg']
                                    color = _colors[70].get('fg', '#000000')
                                elif val >= _thresholds[3]:     # >= 50
                                    bg_color = _colors[50]['bg']
                                    color = _colors[50].get('fg', '#ffffff')
                                else:                           # < 50
                                    bg_color = _colors[0]['bg']
                                    color = _colors[0].get('fg', '#ffffff')
                                sb_html += f'      <td style="background-color:{bg_color}; color:{color}; font-weight:bold; width:70px; min-width:70px; max-width:70px;">{val:.1f}</td>\n'
                        sb_html += '    </tr>\n'
                    sb_html += '  </tbody>\n'
                    sb_html += '</table>\n'
                    score_board_html = sb_html

                    # ==================== Inline Table HTML 렌더링 (Manual) ====================
                    inlinedata_filtered_pivot = inlinedata_filtered_pivot.reset_index()
                    
                    cols = ['Module', 'Item'] + [c for c in inlinedata_filtered_pivot.columns if c not in ['Module', 'Item', 'STEP_DESC_ITEM_ID']]
                    inlinedata_filtered_pivot = inlinedata_filtered_pivot[cols]
                    
                    it_html = '<table class="inline-table">\n'
                    it_html += '  <thead>\n'
                    it_html += '    <tr>\n'
                    for col in inlinedata_filtered_pivot.columns:
                        if col in ['Module', 'Item']:
                            it_html += f'      <th class="row_heading" style="background-color:#e2efda !important;">{col}</th>\n'
                        elif col in ['UCL', 'CL', 'LCL']:
                            it_html += f'      <th style="background-color:#f0f0f0 !important;">{col}</th>\n'
                        else:
                            col_str = str(col) if str(col).startswith('#') else '#' + str(col)
                            it_html += f'      <th style="background-color:#f0f0f0 !important; width:70px; min-width:70px; max-width:70px;">{col_str}</th>\n'
                    it_html += '    </tr>\n'
                    it_html += '  </thead>\n'
                    it_html += '  <tbody>\n'
                    for _, row in inlinedata_filtered_pivot.iterrows():
                        it_html += '    <tr>\n'
                        for col in inlinedata_filtered_pivot.columns:
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
                            
                            style = ""
                            if col in ['UCL', 'CL', 'LCL']:
                                style = 'background-color:#e0f7fa;'
                            elif col in ['Module', 'Item']:
                                style = 'background-color:#f0fff4;'
                            else:
                                style = 'width:70px; min-width:70px; max-width:70px;'
                            
                            it_html += f'      <td style="{style}">{formatted_val}</td>\n'
                        it_html += '    </tr>\n'
                    it_html += '  </tbody>\n'
                    it_html += '</table>\n'
                    inline_table_html = it_html

                    # ==================== Lot Detail Table HTML 렌더링 ====================
                    et_log_styled = et_log.style.format(na_rep="").hide(axis='index')
                    lot_detail_html = et_log_styled.set_table_attributes('class="lot-detail-table"').to_html()

                    # ==================== Anomaly Detection & GPT 요약 ====================
                    # GPT 연동 기능은 My_config.py의 플래그로 ON/OFF 제어합니다.
                    #   - GLOBAL_CONFIG.use_gpt_summary      : GPT 요약(Top 항목 선정/요약문)
                    #   - GLOBAL_CONFIG.use_gpt_anomaly_chart: GPT 선정 항목 기반 이상차트(3x2)
                    # False면 해당 GPT 기능 호출 자체를 하지 않고 완전히 스킵합니다.
                    anomaly_html = ""
                    gpt_summary_html = ""
                    top_item_names = []

                    # 1. GPT OSS 120B 우회 호출 (Top 6 선정 및 요약)
                    if GLOBAL_CONFIG.use_gpt_summary:
                        try:
                            gpt_summary_html, top_item_names = generate_report_summary(metrics_dict)
                        except Exception as ae:
                            print(f"[WARN] GPT 요약 스킵 (오류): {ae}")
                    else:
                        print("[INFO] use_gpt_summary=False → GPT 요약 스킵")

                    # 2. HTML 3x2 Grid 이상차트 생성 (GPT가 선정한 top_item_names 기반)
                    if GLOBAL_CONFIG.use_gpt_anomaly_chart:
                        try:
                            import base64
                            if top_item_names:
                                anomaly_html = '<div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;">'
                                for item in top_item_names:
                                    img_path = f"RUN/TEMP/{item}.png"
                                    if os.path.exists(img_path):
                                        with open(img_path, "rb") as f:
                                            img_b64 = base64.b64encode(f.read()).decode('utf-8')
                                        anomaly_html += f'<div style="text-align:center;"><img src="data:image/png;base64,{img_b64}" style="max-width:100%; border:1px solid #ddd;"/><br><b>{item}</b></div>'
                                anomaly_html += '</div>'
                            else:
                                anomaly_html = '<p>이상항목 없음</p>'
                        except Exception as ae:
                            print(f"[WARN] 이상차트 생성 스킵 (오류): {ae}")
                    else:
                        print("[INFO] use_gpt_anomaly_chart=False → 이상차트 스킵")

                    # ==================== HTML 조립 ====================
                    sub_title = f'{target_lot_id} / {target_step_merged}'
                    html_content = html_code.replace('sub_title', sub_title)

                    html_content = html_content.replace(
                        '<div id="target0"></div>',
                        f'<div id="target0"><div class="section-title">■ [0] Anomaly Summary</div>{gpt_summary_html}<br>{anomaly_html}</div>'
                    )
                    html_content = html_content.replace(
                        '<div id="target1"></div>',
                        f'<div id="target1"><div class="section-title">■ [1] Score Board</div>'
                        f'<div class="table-container">{score_board_html}</div></div>'
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
                    if S3_CONNECT and client:
                        try:
                            client.upload_file(
                                f'{low_qual_ppt_save_path}{final_ppt_file_name_DX}',
                                bucket_dx,
                                f'{vehicle}/{final_ppt_file_name_DX}'
                            )
                            print(f'[INFO] S3 업로드 완료: {final_ppt_file_name_DX}')
                        except Exception as s3e:
                            print(f'[WARN] S3 업로드 스킵: {s3e}')

                    log_to_file(f"{search_key} Report 발행 완료", query_log)
                    print(f'[INFO] *****{search_key} AUTO LOT Report 발행 완료*****')

                except Exception as e:
                    print(f'[ERROR] {search_key} Report 발행 실패: {e}')
                    traceback.print_exc()
                    log_to_file(f"{search_key} Report 발행 실패: {e}", error_log)
                    continue

                finally:
                    clear_temp_inside_run()
                    clear_anomaly_inside_run()
                    gc.collect()

        else:
            print("[INFO] dc_done_list가 비어있습니다. Report 발행 대상 없음")

    else:
        print(f"[INFO] DB_Setting_mode = {DB_Setting_mode}, report_making = {report_making}")
        print("[INFO] Report 미발행 모드")

    conn.close()
    print(f'[INFO] ============== {vehicle} 전체 프로세스 완료 ==============')

else:
    print("[ERROR] reformatter 검증 실패. 프로그램 종료.")
