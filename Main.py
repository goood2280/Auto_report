import gc  
import io  
import inspect  
import os  
import re  
import sys  
import time  
import traceback  
from datetime import datetime, timedelta  
import warnings

import boto3  
import duckdb  
import numpy as np  
import pandas as pd  
import requests

from bigdataquery import *  
from My_Function import *  
from My_config import GLOBAL_CONFIG

warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")  
warnings.filterwarnings("ignore", category=RuntimeWarning)  
warnings.filterwarnings("ignore", category=FutureWarning)

if len(sys.argv) != 2:  
    print("Usage: python main.py <ItemName>")  
    sys.exit(1)

raw_arg = sys.argv[1]  
trigger_flag = False

if raw_arg.startswith("_TRIGGER_"):  
    raw_arg = raw_arg.replace("_TRIGGER_", "", 1)  
    target_id = raw_arg.strip().split("_")[0]  
    trigger_flag = True  
else:  
    target_id = raw_arg

GLOBAL_CONFIG.load_from_yaml(target_id)

# --- Config Mapping ---  
cfg = {  
    'vehicle': GLOBAL_CONFIG.get("vehicle"),  
    'prod': GLOBAL_CONFIG.get("prod"),  
    'viewing_period': int(GLOBAL_CONFIG.get("viewing_period")),  
    'delay_min': GLOBAL_CONFIG.get("delay_min"),  
    'test_mode': GLOBAL_CONFIG.get("test_mode"),  
    'DB_Setting_mode': GLOBAL_CONFIG.get("DB_Setting_mode"),  
    'report_making': GLOBAL_CONFIG.get("report_making"),  
    'ROOT': GLOBAL_CONFIG.get("ROOT"),  
    'DB_path': GLOBAL_CONFIG.get("DB"),  
    'pivot_raw_path': GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw"),  
    'log_path': GLOBAL_CONFIG.get("log"),  
    'report_path': GLOBAL_CONFIG.get("Report"),  
    'ppt_low_path': GLOBAL_CONFIG.get("low_qual_ppt_save_path"),  
    'ppt_high_path': GLOBAL_CONFIG.get("high_qual_ppt_save_path"),  
    'html_path': GLOBAL_CONFIG.get("html_save_path"),  
    'et_log_path': GLOBAL_CONFIG.get("et_log_path"),  
    'final_log_path': GLOBAL_CONFIG.get("Final_et_log_path"),  
    'reformatter_path': f'reformatter/{target_id}_reformatter.csv',  
    'coord_path': GLOBAL_CONFIG.get("coordinate_file_path"),  
    'inline_path': GLOBAL_CONFIG.get("inline_file_path"),  
    'template_ppt': GLOBAL_CONFIG.get("template_ppt_path"),  
    'desc_ppt': GLOBAL_CONFIG.get("description_ppt_path"),  
}

for path in [cfg['ROOT'], cfg['DB_path'], cfg['log_path'], cfg['report_path'], cfg['ppt_low_path'], cfg['ppt_high_path'], cfg['html_path']]:  
    if not os.path.exists(path):  
        os.makedirs(path)

s3_client = boto3.client(  
    service_name='s3', region_name='DS',  
    aws_access_key_id=GLOBAL_CONFIG.get("s3_aws_access_key_id"),  
    aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),  
    endpoint_url='url'  
)

now = datetime.now()  
upload_date = now.strftime('%Y%m%d')

df_reformatter = pd.read_csv(cfg['reformatter_path'])  
if reformatter_verify(df_reformatter):  
    db_conn = duckdb.connect()

    if not cfg['test_mode'] and not trigger_flag:  
        etdata_query()  
      
    if not GLOBAL_CONFIG.get("target_lot"):  
        et_LOTWF_generator()  
      
    wipdata_query()

    # --- Log Processing ---  
    df_et_log = pd.read_csv(cfg['et_log_path'])  
    df_existing_log = pd.read_csv(cfg['final_log_path']) if os.path.exists(cfg['final_log_path']) else pd.DataFrame(columns=['prime_key','wafer_id','step_seq','total_site_cnt','tkout_time','lot_id','dc_step_id','dc_done'])  
      
    df_wip = pd.read_csv(cfg['DB_path'] + f'{cfg["vehicle"]}_wip_current.csv', encoding='cp949')  
    df_wip['last_update_date'] = pd.to_datetime(df_wip['last_update_date'])  
    df_wip_grouped = df_wip.groupby('lot_id').last().reset_index()

    df_et_log['lot_id'] = df_et_log['prime_key'].str.split('_').str[1]  
    df_et_log['dc_step_id'] = df_et_log['prime_key'].str.split('_').str[2]  
    df_et_log = pd.merge(df_et_log, df_wip_grouped[['lot_id','step_id']], on='lot_id', how='left')

    df_combined_log = pd.concat([df_existing_log, df_et_log])  
    df_final_log = df_combined_log.drop_duplicates(subset=['prime_key'], keep='last')  
    df_final_log['tkout_time'] = pd.to_datetime(df_final_log['tkout_time'])

    # --- Completion Logic ---  
    threshold_time = now - timedelta(minutes=cfg['delay_min'])  
    df_final_log['dc_step_id_num'] = df_final_log['dc_step_id'].str.extract('(\d+)', expand=False).astype(float)  
    df_final_log['step_id_num'] = df_final_log['step_id'].str.extract('(\d+)', expand=False).astype(float)  
      
    df_final_log['dc_done'] = np.where(  
        ((df_final_log['step_id'].str[:2] != df_final_log['dc_step_id'].str[:2]) |   
         (df_final_log['step_id'].isnull()) |   
         (df_final_log['step_id_num'] - df_final_log['dc_step_id_num'] >= 100)) &   
        (threshold_time > df_final_log['tkout_time']), True, False  
    )  
    df_final_log.to_csv(cfg['final_log_path'], index=False)

    # --- Target Extraction ---  
    df_selected_log = df_final_log[['lot_id', 'dc_step_id', 'dc_done','tkout_time']]  
    df_selected_before = df_existing_log[['lot_id', 'dc_step_id', 'dc_done']].rename(columns={'dc_done': 'dc_done_before'})  
    df_selected_log = pd.merge(df_selected_log, df_selected_before, on=['lot_id','dc_step_id'], how='left')  
      
    df_done_list = df_selected_log[(df_selected_log['dc_done'] != df_selected_log['dc_done_before']) & (df_selected_log['dc_done'] == True)]

    if trigger_flag:  
        df_done_list = pd.DataFrame({  
            'lot_id': [raw_arg.strip().split("_")[1]],  
            'dc_step_id': [raw_arg.strip().split("_")[2]],  
            'dc_done': [True],  
            'dc_done_before': [False]  
        })

    if (not cfg['DB_Setting_mode']) and (cfg['report_making']):  
        if not df_done_list.empty:  
            df_done_list['search_key'] = df_done_list['lot_id'].astype(str) + '_' + df_done_list['dc_step_id'].astype(str)  
            search_keys = df_done_list['search_key'].unique().tolist()  
              
            parquet_files = [os.path.join(cfg['pivot_raw_path'], f) for f in os.listdir(cfg['pivot_raw_path']) if f.endswith('.parquet')]  
              
            # DuckDB Query Execution  
            # [Simplified SQL Logic for max values and base data]  
            query_sql = f"SELECT * FROM read_parquet([{', '.join([f"'{f}'" for f in parquet_files])}], union_by_name=True) WHERE tkout_time >= (CURRENT_DATE - INTERVAL '{cfg['viewing_period']}' DAY)"  
            df_merged = db_conn.execute(query_sql).df()

            # --- Column Filtering & Preprocessing ---  
            cols_include = df_reformatter[['ALIAS','REPORT ORDER']].dropna(subset=['REPORT ORDER'])['ALIAS'].tolist()  
            cols_base = ['fab_lot_id','lot_id','mask','lot_wf','root_lot_id','wafer_id','process_id','part_id','step_id','step_seq','tkout_time','flat_zone','eqp_id','probe_card_id','chip_x_pos','chip_y_pos','subitem_id','temperature','total_site_cnt']  
            df_merged = df_merged[[c for c in (cols_include + cols_base) if c in df_merged.columns]]  
              
            df_merged['wafer_id'] = df_merged['wafer_id'].astype(int)  
            df_merged['DC_Split'] = df_merged['step_id'].replace(GLOBAL_CONFIG.get("dc_dict"))  
            df_merged['search_key'] = df_merged['fab_lot_id'].astype(str) + "_" + df_merged['step_id'].astype(str)  
            df_merged['match_key'] = df_merged['root_lot_id'].astype(str) + "_" + df_merged['step_id'].astype(str)  
            df_merged['tkout_time'] = pd.to_datetime(df_merged['tkout_time'])  
              
            # --- Coordinate & Zone Mapping ---  
            df_coord = pd.read_excel(cfg['coord_path'], sheet_name=None)  
            df_zone = df_coord['Zone_Define']  
            df_merged = pd.merge(df_merged, df_zone, on=['MASK','CHIP_X_POS','CHIP_Y_POS','FLAT_ZONE_POS'])

            # --- Reporting Loop ---  
            for skey in search_keys:  
                try:  
                    target_lot = skey.split('_')[0]  
                    target_root = target_lot[:5]  
                    target_step_id = skey.split('_')[1]  
                      
                    df_target = df_merged[df_merged['match_key'] == (target_root + "_" + target_step_id)]  
                    if df_target.empty: continue  
                      
                    df_inline = inlinedata_query(target_root)  
                      
                    # Pass Rate Calculation  
                    spec_dict = df_reformatter.set_index('ALIAS')[['SPECLOW', 'SPECHIGH']].to_dict('index')  
                    # [Pass rate logic applied to df_target]  
                      
                    # VIP Grouping (Scoreboard)  
                    # [Pivot and grouping logic]  
                    df_vip_group = pd.DataFrame()   
                      
                    # PPT Generation  
                    prs = make_title_page(cfg['template_ppt'], cfg['vehicle'], target_lot, target_step_id)  
                    prs = insert_score_board(df_vip_group, prs, target_lot, skey)  
                    prs = insert_plots(df_merged, prs, {}, target_lot, target_root, target_step_id, spec_dict)  
                      
                    ppt_name = f"{upload_date}-{cfg['prod']}-{target_root}-Report.pptx"  
                    prs.save(f"{cfg['ppt_low_path']}/{ppt_name}")  
                      
                    # HTML Generation & Mailing  
                    html_content = GLOBAL_CONFIG.get("html_code").replace('sub_title', f'{target_lot} Summary')  
                    # [HTML Styling and Table Insertion]  
                      
                    email_list = get_email_list(GLOBAL_CONFIG.get("email_list_path"), "RECEIVER_GROUP")  
                    requests.post(GLOBAL_CONFIG.get("url"),   
                                  headers={'x-dep-ticket': GLOBAL_CONFIG.get("TICKET")},   
                                  data={'mailSendString': 'payload'},   
                                  files=[('file', (ppt_name, open(f"{cfg['ppt_low_path']}/{ppt_name}","rb"), 'application/vnd.ms-powerpoint'))])  
                      
                    # S3 Upload  
                    s3_client.upload_file(f"{cfg['ppt_low_path']}/{ppt_name}", "BUCKET_DX", f"HOL_Report/{ppt_name}")

                except Exception as e:  
                    log_to_file(f"Error processing {skey}: {e}", cfg['log_path'])

    db_conn.close()  
    gc.collect()  
else:  
    print("Reformatter verification failed.")  
