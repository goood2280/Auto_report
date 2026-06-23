# ----- Python 표준 라이브러리  
import gc  
import os  
import sys  
import traceback  
from datetime import datetime, timedelta  
import warnings

# ----- 서드‑파티  
import boto3  
import duckdb  
import numpy as np  
import polars as pl  
import pandas as pd
import requests

# ----- 프로젝트 내부 모듈  
from bigdataquery import *  
from My_Function import *  
from My_config import GLOBAL_CONFIG

# ==================================================================================================================================

import warnings
warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
# polars does not have SettingWithCopyWarning

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
bucket_dx = 'DX.Archive' 
bucket_simyung = 'simyung.woo'

# Download the file
client = boto3.client(
            service_name='s3', region_name='DS',
            aws_access_key_id=GLOBAL_CONFIG.get("s3_aws_access_key_id"),  
            aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),
            endpoint_url=GLOBAL_CONFIG.get("s3_endpoint_url"))

datetime_now = datetime.now()
formatted_datetime = datetime_now.strftime('%y-%m-%d-%H-%M')
upload_date = datetime_now.strftime('%Y%m%d')

reformatter = pl.read_csv(f'reformatter/{vehicle}_reformatter.csv') 

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

    et_log = pl.read_csv(et_log_path) # n일 치 et_log
    existing_lot_log = pl.read_csv(Final_et_log_path) if os.path.exists(Final_et_log_path) else pl.DataFrame()

    wip_current = pl.read_csv(DB + f'{vehicle}_wip_current.csv' ,encoding='cp949')
    wip_current = wip_current.with_columns(pl.col('last_update_date').str.to_datetime())
    wip_current = wip_current.sort('last_update_date')
    grouped = wip_current.group_by('lot_id').last()

    et_log = et_log.with_columns(
        pl.col('prime_key').str.split('_').list.get(1).alias('lot_id'),
        pl.col('prime_key').str.split('_').list.get(2).alias('dc_step_id')
    )
    et_log = et_log.join(grouped.select(['lot_id','step_id']), on='lot_id', how='left')

    combined_lot_log = pl.concat([existing_lot_log, et_log]) 
    final_lot_log = combined_lot_log.unique(subset=['prime_key'], keep='last') #기존 et_log update
    final_lot_log = final_lot_log.with_columns(pl.col('tkout_time').str.to_datetime())

    datetime_now_plus = datetime_now - timedelta(minutes=delay_min) 

    # LOT 완료 확인 Logic
    final_lot_log = final_lot_log.with_columns(
        pl.col('dc_step_id').str.extract(r'(\d+)', 1).cast(pl.Float64).alias('dc_step_id_num'),
        pl.col('step_id').str.extract(r'(\d+)', 1).cast(pl.Float64).alias('step_id_num')
    )
    final_lot_log = final_lot_log.with_columns(
        pl.when(
            (pl.col('step_id').str.slice(0, 2) != pl.col('dc_step_id').str.slice(0, 2)) |
            pl.col('step_id').is_null() |
            ((pl.col('step_id_num') - pl.col('dc_step_id_num')) >= 100)
        ).then(
            pl.when(datetime_now_plus > pl.col('tkout_time')).then(True).otherwise(False)
        ).otherwise(False).alias('dc_done')
    )
                                        
    # Report 1 회만 발송
    # dc_done 열에서 True 값을 유지하기 위해 원본 데이터프레임에서 True 값이 있는경우 그대로 반영
    max_dc_done = combined_lot_log.group_by('prime_key').agg(pl.col('dc_done').max())
    final_lot_log = final_lot_log.join(max_dc_done, on='prime_key', how='left')
    final_lot_log = final_lot_log.with_columns(
        pl.col('dc_done').fill_null(pl.col('dc_done_right')).alias('dc_done')
    ).drop('dc_done_right')

    final_lot_log = final_lot_log.drop(['step_id', 'dc_step_id_num', 'step_id_num'])
    final_lot_log = final_lot_log.sort('tkout_time')
    final_lot_log.write_csv(Final_et_log_path)

    selected_et_log = final_lot_log[['lot_id', 'dc_step_id', 'dc_done','tkout_time']].copy()
    selected_et_log_before = existing_lot_log[['lot_id', 'dc_step_id', 'dc_done']].copy()
    selected_et_log_before = selected_et_log_before.rename({'dc_done': 'dc_done_before'})
    selected_et_log = selected_et_log.join(selected_et_log_before, on=['lot_id','dc_step_id'], how='left')

    # DC 완료여부 판정 Logic
    dc_done_list = selected_et_log[(selected_et_log['dc_done'] != selected_et_log['dc_done_before'])]
    dc_done_list = dc_done_list[dc_done_list['dc_done'] == True]
    
    if not trigger_flag:
        print("\n" + "=" * 80)
        print(f"[INFO] {datetime_now} 측정완료 LOT List")
        print("=" * 80)
        print("[INFO] 측정완료된 dc_done_list:")
        print(dc_done_list.to_string(index=False))
        print("=" * 80 + "\n")
    
    if ptype_lot_turnoff == True or ptype_lot_turnoff == 'True' :
        print("\n" + "=" * 80)
        print("[INFO] ptype_lot_turnoff True 로 ptype 제외")
        print("=" * 80)
        dc_done_list = dc_done_list[~dc_done_list['lot_id'].str.startswith('A4')]
        print("[INFO] A4* 자재 제외 List:")
        print(dc_done_list.to_string(index=False))
        print("=" * 80 + "\n")
    
    if specific_dc_layer is not False:
        print("\n" + "=" * 80)
        print("[INFO] specific_dc_layer 만 Report 생성됨")
        print("=" * 80)
        dc_done_list['dc_layer_check'] = dc_done_list['dc_step_id'].replace(GLOBAL_CONFIG.get("dc_dict"), default=None)
        dc_done_list = dc_done_list.filter(pl.col('dc_layer_check') == 'MFDC')
        dc_done_list = dc_done_list.drop('dc_layer_check')
        print("[INFO] specific_dc_layer 외 제외 List:")
        print(dc_done_list.to_string(index=False))
        print("=" * 80 + "\n")
    

    if trigger_flag :
        #trigger
        dc_done_list = {
            'lot_id': [raw_arg.strip().split("_")[1]],
            'dc_step_id': [raw_arg.strip().split("_")[2]],
            'dc_done': [True],
            'dc_done_before': [False]
        }

    if trigger_flag:
        print("\n" + "=" * 80)
        print("[INFO] 강제발행모드입니다. 쿼리 수행되지않고 현재 DB 에서 리포팅만 실행합니다.")
        print("=" * 80 + "\n")
    
    print("\n" + "=" * 80)
    print("[INFO] 리포팅 진행할 LOT LIST")
    print("=" * 80)
    dc_done_list = pl.DataFrame(dc_done_list)

    if (not DB_Setting_mode) & (report_making):
        print(f"\n[INFO] DB_Setting_mode =  {DB_Setting_mode}")
        print(f"[INFO] report_making = {report_making}")
        if dc_done_list.height > 0:
            
            #dc_done_list
            dc_done_list = dc_done_list.with_columns(
                (pl.col('lot_id').cast(pl.Utf8) + '_' + pl.col('dc_step_id').cast(pl.Utf8)).alias('search_key')
            )
            search_strings = dc_done_list['search_key'].unique().to_list() #측정된 {fab_lot_id}_{dc_step_id} list
            
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

            # SELECT 구문
            select_other = ",\n    ".join([f"b.{q(c)}" for c in other_cols])
            select_vramp = ",\n    ".join([
                f'CASE WHEN b.{q(c)} IS NOT NULL AND b.{q(c)} = b.{q(c)} '
                f'THEN m.{q(c)} ELSE b.{q(c)} END AS {q(c)}'
                for c in vramp_cols
            ])
            max_selects = ",\n        ".join([f"MAX({q(c)}) AS {q(c)}" for c in vramp_cols])
            select_clause = f"{select_other},\n    {select_vramp}"

            # 그룹 키
            group_keys = ["root_lot_id", "wafer_id", "chip_x_pos", "chip_y_pos"]   # 필요 시 수정
            group_keys_sql = ", ".join([q(k) for k in group_keys])
            join_cond = " AND ".join([f"b.{q(k)} = m.{q(k)}" for k in group_keys])

            if GLOBAL_CONFIG.get("target_lot") : 
                target_lot_list = [c[:5] for c in dc_done_list['lot_id'].to_list()]
                et_total_query = f"""
                        WITH base AS (
                                SELECT *
                                FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files])}], union_by_name=True)
                                WHERE root_lot_id IN {target_lot_list}
                            ),
                            maxvals AS (
                                SELECT
                                    {group_keys_sql},
                                    {max_selects}
                                FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files])}], union_by_name=True)
                                WHERE tkout_time >= (CURRENT_DATE - INTERVAL '365' DAY)
                                GROUP BY {group_keys_sql}
                            )
                            SELECT
                                {select_clause}
                            FROM base b
                            LEFT JOIN maxvals m
                                ON {join_cond};
                """
            else : 
                et_total_query = f"""
                            WITH base AS (
                                    SELECT *
                                    FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files])}], union_by_name=True)
                                    WHERE tkout_time >= (CURRENT_DATE - INTERVAL '{viewing_period}' DAY)
                                ),
                                maxvals AS (
                                    SELECT
                                        {group_keys_sql},
                                        {max_selects}
                                    FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files])}], union_by_name=True)
                                    WHERE tkout_time >= (CURRENT_DATE - INTERVAL '365' DAY)
                                    GROUP BY {group_keys_sql}
                                )
                                SELECT
                                    {select_clause}
                                FROM base b
                                LEFT JOIN maxvals m
                                    ON {join_cond};
                """

            merged_df = conn.execute(et_total_query).df()
            #with_vehicle_table load & Merge
            if not vehicle in with_vehicle :
                print("[INFO] with_vehicle안에 vehicle 없음. 진행")
                try : 
                    with_vehicle_Table = pl.DataFrame() 
                    for with_vehicle_now in with_vehicle :
                        ET_TABLE_ROOT_with_vehicle  = DB + with_vehicle_now + '_LOTWF/pivot_raw/'
                        pattern_with_vehicle = os.path.join(ET_TABLE_ROOT_with_vehicle, f'*')
                        
                        print(f'[INFO] viewing_period = {viewing_period}')
                        # 특정 경로의 모든 Parquet 파일을 DuckDB 테이블로 읽기
                        parquet_files_with_vehicle = [os.path.join(ET_TABLE_ROOT_with_vehicle, f) for f in os.listdir(ET_TABLE_ROOT_with_vehicle) if f.endswith('.parquet')]
                        
                        # 스키마 읽기
                        cols_df_with_vehicle = conn.execute(f"""
                            SELECT * 
                            FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files_with_vehicle])}], union_by_name=True)
                            LIMIT 0
                        """).fetchdf()
                        all_cols_with_vehicle = list(cols_df_with_vehicle.columns)
                        vramp_cols_with_vehicle = [c for c in all_cols_with_vehicle if "VRAMP" in c]
                        other_cols_with_vehicle = [c for c in all_cols_with_vehicle if c not in vramp_cols_with_vehicle]

                        # SELECT 구문
                        select_other_with_vehicle = ",\n    ".join([f"b.{q(c)}" for c in other_cols_with_vehicle])
                        select_vramp_with_vehicle = ",\n    ".join([
                            f'CASE WHEN b.{q(c)} IS NOT NULL AND b.{q(c)} = b.{q(c)} '
                            f'THEN m.{q(c)} ELSE b.{q(c)} END AS {q(c)}'
                            for c in vramp_cols_with_vehicle
                        ])
                        max_selects_with_vehicle = ",\n        ".join([f"MAX({q(c)}) AS {q(c)}" for c in vramp_cols_with_vehicle])
                        select_clause_with_vehicle = f"{select_other_with_vehicle},\n    {select_vramp_with_vehicle}"

                        et_total_query_with_vehicle = f"""
                                    WITH base AS (
                                            SELECT *
                                            FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files_with_vehicle])}], union_by_name=True)
                                            WHERE tkout_time >= (CURRENT_DATE - INTERVAL '{viewing_period}' DAY)
                                        ),
                                        maxvals AS (
                                            SELECT
                                                {group_keys_sql},
                                                {max_selects_with_vehicle}
                                            FROM read_parquet([{', '.join([f"'{file}'" for file in parquet_files_with_vehicle])}], union_by_name=True)
                                            WHERE tkout_time >= (CURRENT_DATE - INTERVAL '365' DAY)
                                            GROUP BY {group_keys_sql}
                                        )
                                        SELECT
                                            {select_clause_with_vehicle}
                                        FROM base b
                                        LEFT JOIN maxvals m
                                            ON {join_cond};
                        """
                        with_vehicle_Table_now = conn.execute(et_total_query_with_vehicle).df()
                        with_vehicle_Table = pl.concat([with_vehicle_Table,pl.from_pandas(with_vehicle_Table_now)], ignore_index=True)

                except :
                    with_vehicle_Table = pl.DataFrame()
                    
                merged_df = pl.concat([merged_df,with_vehicle_Table], ignore_index=True)
            
            df_include_column = reformatter.select(['ALIAS','REPORT ORDER']).drop_nulls(subset=['REPORT ORDER']).drop('REPORT ORDER')
            columns_to_include_1 = df_include_column['ALIAS'].to_list()
            columns_to_include_2 = ['fab_lot_id','lot_id','mask','lot_wf','root_lot_id','wafer_id','process_id','part_id','step_id','step_seq'\
                                        ,'tkout_time','flat_zone','eqp_id','probe_card_id','chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt']
            columns_to_include = columns_to_include_1 +  columns_to_include_2
            filtered_columns = [col for col in columns_to_include if col in merged_df.columns]
            merged_df = merged_df.select(filtered_columns)

            columns_to_exclude_1 = [col for col in merged_df.columns if 'PCHK' in col]
            columns_to_exclude_2 = ['fab_lot_id','lot_id','mask','lot_wf','root_lot_id','wafer_id','process_id','part_id','step_id','step_seq'\
                                    ,'tkout_time','flat_zone','eqp_id','probe_card_id','chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt']
            columns_to_exclude = columns_to_exclude_1 +  columns_to_exclude_2
            columns_to_check = [col for col in merged_df.columns if col not in columns_to_exclude]
            merged_df = merged_df.drop_nulls(subset=columns_to_check)

            merged_df = merged_df.with_columns(
                pl.col('wafer_id').cast(pl.Int64),
                pl.col('step_id').replace_strict(GLOBAL_CONFIG.get("dc_dict"), default=None).alias('DC_Split'),
                (pl.col('fab_lot_id').cast(pl.Utf8) + '_' + pl.col('step_id').cast(pl.Utf8)).alias('search_key'),
                (pl.col('root_lot_id').cast(pl.Utf8) + '_' + pl.col('step_id').cast(pl.Utf8)).alias('match_key')
            )
            merged_df = merged_df.with_columns(pl.col('tkout_time').str.to_datetime())
            
            # Change data type
            merged_df = merged_df.select([
                pl.col('wafer_id').cast(pl.Int64),
                pl.col('chip_x_pos').cast(pl.Int64),
                pl.col('chip_y_pos').cast(pl.Int64),
                pl.col('flat_zone').cast(pl.Int64),
                pl.col('temperature').cast(pl.Float64)
            ])

            # Add TEMPERATURE Modified
            merged_df = merged_df.with_columns(
                (pl.col('temperature') / 5).round(0).cast(pl.Int64) * 5
            )
            # =====================================================================================================

            # Add coordinate_file
            coordinate_file = pl.read_excel(coordinate_file_path)
            zone_define = coordinate_file['Zone_Define'] if isinstance(coordinate_file, dict) else coordinate_file
            zone_define = zone_define.with_columns(
                pl.col('MASK').str.replace('RHV_OS', 'RHV-OS')
            ) #RHV OS Vehicle 명 상이함. matching을 위한 변경
            zone_define = zone_define.select([
                pl.col('CHIP_X_POS').cast(pl.Int64),
                pl.col('CHIP_Y_POS').cast(pl.Int64),
                pl.col('CHIP_X_ADJ').cast(pl.Int64),
                pl.col('CHIP_Y_ADJ').cast(pl.Int64),
                pl.col('FLAT_ZONE_POS').cast(pl.Int64)
            ])

            # =====================================================================================================

            # Add Point column
            merged_df = merged_df.with_columns(pl.lit(1).alias('Point'))
            merged_df = merged_df.with_columns(
                pl.col('Point').over(['fab_lot_id','wafer_id','tkout_time']).cast(pl.Utf8)
            )

            # Add duplicate count
            merged_df = merged_df.with_columns(
                pl.col('tkout_time').rank(method='dense').over(['DC_Split','temperature','flat_zone','fab_lot_id','wafer_id','step_seq','Point']).alias('Duplicate_Count')
            )

            # Add PGM(pt)_CNT
            merged_df = merged_df.with_columns(
                (pl.col('step_seq').cast(pl.Utf8) + '(' + pl.col('Point') + 'pt)_' + pl.col('Duplicate_Count').cast(pl.Utf8)).alias('PGM(pt)')
            )

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
            rename_map = {}
            for col in merged_df.columns:

