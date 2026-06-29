# ----- Python 표준 라이브러리
import gc
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta
import warnings

# ----- 서드파티
import boto3
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
    vehicle_name = raw_arg.strip().split("_")[0]
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
high_qual_ppt_mode = GLOBAL_CONFIG.get("high_qual_ppt_mode")
KNOXID = GLOBAL_CONFIG.get("KNOXID")
user_name = GLOBAL_CONFIG.get("user_name")
email_receiver = GLOBAL_CONFIG.get("email_receiver")

ROOT = GLOBAL_CONFIG.get("ROOT")
DB = GLOBAL_CONFIG.get("DB")
DB_et_daily = GLOBAL_CONFIG.get("DB_et_daily")
DB_et_LOTWF_raw = GLOBAL_CONFIG.get("DB_et_LOTWF_raw")
DB_et_LOTWF_pivot_raw = GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw")
Report = GLOBAL_CONFIG.get("Report")
low_qual_ppt_save_path = GLOBAL_CONFIG.get("low_qual_ppt_save_path")
high_qual_ppt_save_path = GLOBAL_CONFIG.get("high_qual_ppt_save_path")
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
ref_turnoff = GLOBAL_CONFIG.get("ref_turnoff")

# =============================================== Folder path 생성 ==================================================================

for target_path in [ROOT, DB, log, Report, low_qual_ppt_save_path, high_qual_ppt_save_path, html_save_path]:
    if not os.path.exists(target_path):
        os.makedirs(target_path)

# =============================================== Main Loop 실행 ====================================================================
bucket_dx = GLOBAL_CONFIG.get("bucket_dx") 
bucket_simyung = GLOBAL_CONFIG.get("bucket_simyung") 

# Download the file
client = boto3.client(
            service_name='s3', region_name='DS',
            aws_access_key_id=GLOBAL_CONFIG.get("s3_aws_access_key_id"),  
            aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),
            endpoint_url=GLOBAL_CONFIG.get("endpoint_url"))

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
        etdata_query()
        print('[INFO] ==============et_query 수행완료==============')
        
        if GLOBAL_CONFIG.get("target_lot"):
            print('[INFO] ==============et_LOTWF_generator 미수행==============')
        else:
            et_LOTWF_generator()
            print('[INFO] ==============et_LOTWF_generator 수행완료==============')
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

    et_log['lot_id'] = et_log['prime_key'].str.split('_').str[1]
    et_log['dc_step_id'] = et_log['prime_key'].str.split('_').str[2]
    et_log = pd.merge(et_log, grouped[['lot_id','step_id']], on='lot_id', how='left')

    combined_lot_log = pd.concat([existing_lot_log, et_log]) 
    final_lot_log = combined_lot_log.drop_duplicates(subset=['prime_key'], keep='last').copy() #기존 et_log update
    final_lot_log['tkout_time'] = pd.to_datetime(final_lot_log['tkout_time'])

    datetime_now_plus = datetime_now - timedelta(minutes=delay_min) 

    # LOT 완료 확인 Logic
    final_lot_log['dc_step_id_num'] = final_lot_log['dc_step_id'].str.extract('(\d+)', expand=False).astype(float)
    final_lot_log['step_id_num'] = final_lot_log['step_id'].str.extract('(\d+)', expand=False).astype(float)

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
        print(f"[INFO] {datetime_now} 측정완료 LOT List")
        print("[INFO] 측정완료된 dc_done_list = ",dc_done_list)
    
    if ptype_lot_turnoff == True or ptype_lot_turnoff == 'True' :
        print("[INFO] ptype_lot_turnoff True로 ptype 제외")
        dc_done_list = dc_done_list[~dc_done_list['lot_id'].str.startswith('A4')]
        print("[INFO] A4* 자재 제외 List = ",dc_done_list)
    
    if specific_dc_layer is not False:
        print("[INFO] specific_dc_layer 만 Report 생성됨")
        dc_done_list['dc_layer_check'] = dc_done_list['dc_step_id'].map(GLOBAL_CONFIG.get("dc_dict"))
        dc_done_list = dc_done_list[dc_done_list['dc_layer_check'] == 'MFDC']
        dc_done_list = dc_done_list.drop(columns=['dc_layer_check'])
        print("[INFO] specific_dc_layer 외 제외List = ",dc_done_list)
    
    # trigger_flag = True

    if trigger_flag :
        #trigger
        dc_done_list = {
            'lot_id': [raw_arg.strip().split("_")[1]],
            'dc_step_id': [raw_arg.strip().split("_")[2]],
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
            
            parquet_files = [os.path.join(DB_et_LOTWF_pivot_raw, f) for f in os.listdir(DB_et_LOTWF_pivot_raw) if f.endswith('.parquet')]

            # ALIAS에 VRAMP가 들어간 경우 1년내 LOT_WF,CHIP_XY 기준 MAX 값으로 치환
            # 스키마 읽기
            cols_df = conn.execute(f"""
                SELECT * 
                FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files])}], union_by_name=True)
                LIMIT 0
            """).fetchdf()
            all_cols = list(cols_df.columns)
            vramp_cols = [c for c in all_cols if "VRAMP" in c]
            other_cols = [c for c in all_cols if c not in vramp_cols]

            # 안전하게 인용 (큰따옴표로 감싸기)
            def q(col: str) -> str:
                return '"' + col.replace('"', '""') + '"'


