def etdata_query() :
    try : 

        item_et = pd.read_csv(f'reformatter/{GLOBAL_CONFIG.get("vehicle")}_reformatter.csv') 
        # mask_table = pd.read_csv('reformatter/VEHICLE_PROCESS.csv')

        # print('필요 파일로드완료')
        #경로생성
        if not os.path.exists(GLOBAL_CONFIG.get("DB_et_daily")):
            os.makedirs(GLOBAL_CONFIG.get("DB_et_daily"))
        if not os.path.exists(GLOBAL_CONFIG.get("DB_et_LOTWF_raw")):
            os.makedirs(GLOBAL_CONFIG.get("DB_et_LOTWF_raw"))
        if not os.path.exists(GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw")):
            os.makedirs(GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw"))
        # print('경로생성완료')

        current_time = datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M")


        sub_datetime_now = datetime.now()
        if GLOBAL_CONFIG.get("now_minus") :
            sub_to_date_time = sub_datetime_now - timedelta(days = GLOBAL_CONFIG.get("now_minus"))
            sub_from_date_time = sub_datetime_now - timedelta(days = GLOBAL_CONFIG.get("QueryTimeSpan"))
        else :
            sub_to_date_time = sub_datetime_now 
            sub_from_date_time = sub_datetime_now - timedelta(days = GLOBAL_CONFIG.get("QueryTimeSpan"))
        dateTo = sub_to_date_time.strftime('%Y-%m-%d')
        dateFrom = sub_from_date_time.strftime('%Y-%m-%d')

        date_format = "%Y-%m-%d"
        start_date = datetime.strptime(dateFrom, date_format)
        end_date = datetime.strptime(dateTo, date_format)
        interval = timedelta(days=GLOBAL_CONFIG.get("SplitTimeSpan"))

        date_list = []
        current_date = start_date
        while current_date < end_date:
            date_list.append(current_date.strftime(date_format))
            current_date += interval

        if date_list[-1] != end_date:
            date_list.append(end_date.strftime(date_format))

        paired_date_list = [[date_list[i], date_list[i+1]] for i in range(len(date_list)-1)]
        print(f"[Date Ranges] {paired_date_list}")

        for paired_dates in paired_date_list :
            dateFrom = (datetime.strptime(paired_dates[0], date_format) + timedelta(days=1)).strftime(date_format)
            dateTo = paired_dates[1]
            print("\n" + "="*60)
            print(f"[Query Setting] {GLOBAL_CONFIG.get('SplitTimeSpan')}일치 Query")
            print(f"[Query Period] {dateFrom} ~ {dateTo}")

            is_real = item_et['CATEGORY'] == 'REAL' 
            is_addp = item_et['CATEGORY'] == 'ADDP' 
            real = item_et[is_real]
            addp = item_et[is_addp]
            real = real.loc[:,['ITEMID', 'ALIAS', 'SCALE FACTOR','ABSOLUTE']]
            ITEMID_List=real.ITEMID.tolist()
            addp = addp.loc[:,['ALIAS', 'ADDP FORM','SCALE FACTOR']]
            addp['addpscale'] = addp['SCALE FACTOR'].astype(str) + '*(' + addp['ADDP FORM'] + ')'
            ALIAS = list(map(str,addp.ALIAS)) #ADDP FORM ALIAS
            FORMULA = list(map(str,addp.addpscale)) #ADDP FORM FOMULA

            start_time = time.time()

            print(f"[Query Start] {GLOBAL_CONFIG.get('vehicle')} {GLOBAL_CONFIG.get('SplitTimeSpan')}일치 Query 시작")
            params = {
                        'table_name': 'eds.f_et_test',
                        'dateFrom': dateFrom, 
                        'dateTo': dateTo, 
                        'process_id' : GLOBAL_CONFIG.get("process_id"),
                        'line_id': GLOBAL_CONFIG.get("line_id"),
                        'item_id' : ITEMID_List,  #ITEM ID_REAL
                        'not_like_conditions': {'subitem_id' : ['Q%','AVG','MAX','MIN','RANGE','STD']}, #불필요 통계치
                        'like_conditions': {'step_seq' : GLOBAL_CONFIG.get("setting_stepseq")}
                        }

            Query_Table_tmp = getData(params, custom_columns=GLOBAL_CONFIG.get("et_custom_columns"), user_name=GLOBAL_CONFIG.get("user_name"))

            print(f"[Query Complete] {GLOBAL_CONFIG.get('vehicle')} {dateFrom} ~ {dateTo} 데이터 추출 완료")

            Query_Table_tmp['tkout_time'] = pd.to_datetime(Query_Table_tmp['tkout_time'])
            Query_Table_tmp['et_value'] = pd.to_numeric(Query_Table_tmp['et_value'], errors='coerce')
            Query_Table_tmp['temperature'] = pd.to_numeric(Query_Table_tmp['temperature'], errors='coerce')
            Query_Table_tmp['et_value'] = Query_Table_tmp['et_value'].abs() #전체 absolute 적용
            # Query_Table_tmp['mask'] = GLOBAL_CONFIG.get("vehicle")
            daily_groups = Query_Table_tmp.groupby(Query_Table_tmp['tkout_time'].dt.date)

            #et_log 파일생성
            if os.path.exists(GLOBAL_CONFIG.get("et_log_path")):
                existing_lot_log = pd.read_csv(GLOBAL_CONFIG.get("et_log_path"))
                #형변환
                existing_lot_log['wafer_id'] = existing_lot_log['wafer_id'] .apply(eval)
                existing_lot_log['step_seq'] = existing_lot_log['step_seq'] .apply(eval)
                existing_lot_log['total_site_cnt'] = existing_lot_log['total_site_cnt'] .apply(eval)
            else:
                existing_lot_log = pd.DataFrame()

            lot_log = Query_Table_tmp.copy()
            lot_log['mask'] = GLOBAL_CONFIG.get("vehicle")
            # lot_log = pd.merge(lot_log, mask_table, on='process_id', how='left')
            lot_log['lot_id6'] = lot_log['lot_id'].str.split('_').str[0]
            Query_Table_tmp['wafer_id'] = pd.to_numeric(Query_Table_tmp['wafer_id'], errors='coerce')
            Query_Table_tmp['total_site_cnt'] = pd.to_numeric(Query_Table_tmp['total_site_cnt'], errors='coerce')
            lot_log['wafer_id'] = lot_log['wafer_id'].astype(int)
            lot_log['total_site_cnt'] = lot_log['total_site_cnt'].astype(int)
            lot_log['tkout_time'] = lot_log['tkout_time'].astype(str)
            lot_log['tkout_time'] = pd.to_datetime(lot_log['tkout_time'], errors='coerce')
            lot_log['prime_key'] = lot_log['mask'].astype(str) + '_' + lot_log['fab_lot_id'].astype(str) + '_' + lot_log['step_id'].astype(str)

            lot_log_unique = lot_log.groupby('prime_key').agg({
                'wafer_id': lambda x: x.unique().tolist(),  
                'step_seq': lambda x: x.unique().tolist(),  
                'total_site_cnt': lambda x: x.unique().tolist(),  
                'tkout_time': 'max'  
            }).reset_index()

            combined_lot_log = pd.DataFrame()
            final_lot_log = pd.DataFrame()
            combined_lot_log = pd.concat([existing_lot_log, lot_log_unique])
            combined_lot_log['tkout_time'] = pd.to_datetime(combined_lot_log['tkout_time'], errors='coerce')
            final_lot_log = combined_lot_log.groupby('prime_key').agg({
                'wafer_id': lambda x: list(sorted(set(sum(x, [])))),
                'step_seq': lambda x: list(sorted(set(sum(x, [])))),
                'total_site_cnt': lambda x: list(sorted(set(sum(x, [])))),
                'tkout_time': 'max' 
            }).reset_index()
            final_lot_log.to_csv(GLOBAL_CONFIG.get("et_log_path"), index = False)

            for date, group in daily_groups:
                output_file_path = f'{GLOBAL_CONFIG.get("DB_et_daily")}'+GLOBAL_CONFIG.get("vehicle")+f'_{date}.parquet'
                group.to_parquet(output_file_path, index=False)

            end_time = time.time()
            elapsed_time = end_time - start_time

            print(f"[ET Query Complete] {GLOBAL_CONFIG.get('vehicle')} ET Query 완료 (소요시간: {elapsed_time:.2f}초)")
            print("="*60 + "\n") 

    except Exception as e:
        print(f"[ERROR] etdata_query 실패: {e}")
        print("[RETRY] 60분 후 재시도합니다...")
        time.sleep(3600)
        etdata_query()

# 사용중 Inline Query
def inlinedata_query(root_lot_id):
    try: 
        setting_file = pd.read_excel(GLOBAL_CONFIG.get("inline_file_path"), sheet_name=None)
        Inline1 = setting_file[GLOBAL_CONFIG.get("inline_file_sheet")]
        Inline1_step_id_list = Inline1['STEP_DESC'].tolist()

        current_time_fab = datetime.now()
        date_To = current_time_fab 
        date_From= current_time_fab - timedelta(days=GLOBAL_CONFIG.get("inline_QueryTimeSpan"))

        params = {
                    'table_name': 'fab.f_fab_wf_met',
                    'dateFrom': date_From.strftime("%Y-%m-%d"),
                    'dateTo': date_To.strftime("%Y-%m-%d"),
                    'process_id' : GLOBAL_CONFIG.get("process_id"),
                    'line_id': GLOBAL_CONFIG.get("line_id"),
                    'root_lot_id' : root_lot_id,
                    'step_seq' : Inline1_step_id_list,
                    'not_like_conditions' : {'subitem_id' : ['SLOTID','MIN','MAX','Q2','RANGE','AVG','STD']}
                    }

        Query_Table = getData(params, custom_columns=GLOBAL_CONFIG.get("inline_custom_columns"), user_name=GLOBAL_CONFIG.get("user_name"))

        Query_Table['fab_value'] = pd.to_numeric(Query_Table['fab_value'], errors='coerce')
        rows_to_multiply = Query_Table[Query_Table['item_id'].str.match(r'^CD\d+$')].index
        Query_Table.loc[rows_to_multiply, 'fab_value'] *= 1000

        Query_Table['spc_ctrl_spec_high'] = pd.to_numeric(Query_Table['spc_ctrl_spec_high'], errors='coerce')
        Query_Table['spc_ctrl_spec_limit'] = pd.to_numeric(Query_Table['spc_ctrl_spec_limit'], errors='coerce')
        Query_Table['spc_ctrl_spec_low'] = pd.to_numeric(Query_Table['spc_ctrl_spec_low'], errors='coerce')

        Query_Table['spc_ctrl_spec_high'] = pd.to_numeric(Query_Table['spc_ctrl_spec_high'], errors='coerce')
        Query_Table['spc_ctrl_spec_limit'] = pd.to_numeric(Query_Table['spc_ctrl_spec_limit'], errors='coerce')
        Query_Table['spc_ctrl_spec_low'] = pd.to_numeric(Query_Table['spc_ctrl_spec_low'], errors='coerce')

        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_high'] *= 1000
        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_limit'] *= 1000
        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_low'] *= 1000

        Query_Table.rename(columns={'step_seq': 'STEP_DESC'}, inplace=True)
        Query_Table = pd.merge(Query_Table, Inline1, on='STEP_DESC', how='left')

        Query_Table.to_csv(GLOBAL_CONFIG.get('DB') + f"{GLOBAL_CONFIG.get('vehicle')}_inline_table.csv", encoding='utf-8-sig', index=False)

        return Query_Table
    
    except Exception as e:
        print(f"inlinedata_query 에러가 발생했습니다: {e}")
        print("[RETRY] 60분 후 재시도합니다...")
        error_trace = traceback.format_exc()
        print(f"에러 상세 정보:\n{error_trace}")
        inlinedata_query(root_lot_id)
        time.sleep(3600)

def wipdata_query():
    try : 
        params = {
                    'table_name': 'fab.f_wip_current',
                    'process_id' : GLOBAL_CONFIG.get("process_id"),
                    'line_id': GLOBAL_CONFIG.get("line_id"),
                    }

        Query_Table_tmp = getData(params, custom_columns=GLOBAL_CONFIG.get("wip_custom_columns"), user_name=GLOBAL_CONFIG.get("user_name"))
        Query_Table_tmp['lot_id6'] = Query_Table_tmp['lot_id'].str.split('.').str[0]
        Query_Table_tmp.rename(columns={'step_seq': 'step_id'}, inplace=True)

        Query_Table_tmp.to_csv(GLOBAL_CONFIG.get('DB') + f"{GLOBAL_CONFIG.get('vehicle')}_wip_current.csv", index = False, encoding='cp949')
            
        print('wip data 추출완료')
    
    except Exception as e:
        print(f"wipdata_query 에러가 발생했습니다: {e}")
        print("[RETRY] 60분 후 재시도합니다...")
        time.sleep(3600)
        wipdata_query()

def et_LOTWF_generator():
    print(f'{GLOBAL_CONFIG.get("vehicle")} 정리중...')

    item_et = pd.read_csv(f'reformatter/{GLOBAL_CONFIG.get("vehicle")}_reformatter.csv') 
    # mask_table = pd.read_csv('reformatter/VEHICLE_PROCESS.csv')

    is_real = item_et['CATEGORY'] == 'REAL' 
    is_addp = item_et['CATEGORY'] == 'ADDP' 
    real = item_et[is_real]
    addp = item_et[is_addp]
    real = real.loc[:,['ITEMID', 'ALIAS', 'SCALE FACTOR','ABSOLUTE']]
    ITEMID_List=real.ITEMID.tolist()
    addp = addp.loc[:,['ALIAS', 'ADDP FORM','SCALE FACTOR']]
    addp['addpscale'] = addp['SCALE FACTOR'].astype(str) + '*(' + addp['ADDP FORM'] + ')'
    ALIAS = list(map(str,addp.ALIAS)) #ADDP FORM ALIAS
    FORMULA = list(map(str,addp.addpscale)) #ADDP FORM FOMULA

    all_files = os.listdir(GLOBAL_CONFIG.get("DB_et_daily"))
    parquet_files = [file for file in all_files if file.endswith('.parquet')] 
    num = 0 #dummy value 몇일완료했는지 확인용

    # 파일 이름에서 날짜를 추출하는 정규표현식 패턴
    date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')

    file_date_df = pd.DataFrame({
        'file_name': parquet_files,
        'date': [date_pattern.search(name).group(1) if date_pattern.search(name) else None for name in parquet_files]
    })

    # 날짜 기준으로 정렬
    file_date_df['date'] = pd.to_datetime(file_date_df['date'])
    file_date_df = file_date_df.sort_values(by='date') #file_date_df = file_date_df.sort_values(by='date', ascending=False) #만약 날짜 역순으로 진행 원할시
    sorted_file_names = file_date_df['file_name'].tolist()
    length_of_list = len(sorted_file_names)

    # 기준 날짜 설정
    base_date = date.today()

    filtered_dates = []

    # 리스트 내의 각 날짜에 대해
    for item in sorted_file_names:
        date_str = item.split('_')[1].split('.')[0] #RHV_OS

        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        if base_date - timedelta(days=GLOBAL_CONFIG.get("QueryTimeSpan") - 1) <= date_obj <= base_date:
            filtered_dates.append(item)

    print(filtered_dates)

    for parquet_file in filtered_dates:
        num = num+1 
        print(parquet_file)
        file_path = os.path.join(GLOBAL_CONFIG.get("DB_et_daily"), parquet_file)
        Query_Table = pd.read_parquet(file_path)

        Query_Table['lot_wf'] = Query_Table['root_lot_id'].astype(str) + "_" + Query_Table['wafer_id'].astype(str)
        # Query_Table = pd.merge(Query_Table, mask_table, on='process_id', how='left')
        Query_Table['mask'] = GLOBAL_CONFIG.get("vehicle")
        
        Query_Table = pd.merge(Query_Table, real, left_on='item_id', right_on='ITEMID', how='left')
        Query_Table['et_value'] = Query_Table['et_value'].astype(float)
        Query_Table['SCALE FACTOR'] = Query_Table['SCALE FACTOR'].astype(float)
        Query_Table['et_value'] = Query_Table['et_value'] * Query_Table['SCALE FACTOR']
        
        Query_Table['item_id'] = Query_Table['ALIAS'] 
        Query_Table['match_key'] = Query_Table['fab_lot_id'].astype(str) + '_' + Query_Table['step_id'].astype(str)

        unique_values = Query_Table['match_key'].unique().tolist()
        print(unique_values)

        # match_key를 group_size개씩 나누어 처리
        group_size = 1
        for i in range(0, len(unique_values), group_size):

            # 현재 그룹의 고유값 선택
            current_group = unique_values[i:i+group_size]
            current_lot = current_group[0] #current_lot matchkey 형식
            print("현재 처리진행 중인 match_key :",current_lot)

            selected_data = Query_Table[Query_Table['match_key'].isin(current_group)]

            # 기존 CSV 파일이 존재하는 경우, 읽어옵니다. 없으면 빈 dataframe 반환
            existing_parquet_path = GLOBAL_CONFIG.get("DB_et_LOTWF_raw") + f'raw_{current_lot}.parquet'
            existing_parquet_path2 = GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw") + f'pivot_raw_{current_lot}.parquet'

            if os.path.exists(existing_parquet_path):
                print(f'기존에 측정된 데이터를 읽어옵니다. {current_lot} : ', existing_parquet_path)
                existing_df = pd.read_parquet(existing_parquet_path)
            else:
                print('기존 s3에 저장된 data가 없습니다.')
                existing_df = pd.DataFrame()

            merged_df = pd.DataFrame()
            merged_df = pd.concat([existing_df, selected_data], ignore_index=True)
            merged_df['et_value'] = merged_df['et_value'].astype(float)

            # 중복 여부를 나타내는 열 추가
            merged_df['is_duplicate'] = merged_df.duplicated(subset=['lot_wf','root_lot_id', 'wafer_id','step_id', 'step_seq',\
                                                                        'tkout_time', 'flat_zone', 'chip_x_pos', 'chip_y_pos', 'total_site_cnt','item_id'])
            duplicate_count = merged_df.duplicated(subset=['lot_wf','root_lot_id', 'wafer_id','step_id', 'step_seq',\
                                                            'tkout_time', 'flat_zone', 'chip_x_pos', 'chip_y_pos', 'total_site_cnt','item_id']).sum()
            print(f"기존 DB에서와 중복된 행의 갯수: {duplicate_count}")
            merged_df = merged_df.drop_duplicates(subset=['lot_wf','root_lot_id', 'wafer_id','step_id', 'step_seq',\
                                                            'tkout_time', 'flat_zone', 'chip_x_pos', 'chip_y_pos', 'total_site_cnt','item_id'])
            merged_df = merged_df.drop('is_duplicate', axis=1)
            merged_df['total_site_cnt'] = merged_df['total_site_cnt'].astype(str)
            merged_df['wafer_id'] = merged_df['wafer_id'].astype(str)
            merged_df.to_parquet(GLOBAL_CONFIG.get("DB_et_LOTWF_raw") + f'raw_{current_lot}.parquet', index=False)
            
            pivoted_data = merged_df.pivot_table(values='et_value', \
                                                index=['fab_lot_id','lot_id', 'mask','lot_wf','root_lot_id', 'wafer_id', 'process_id', 'part_id','step_id', 'step_seq',\
                                                        'tkout_time', 'flat_zone', 'eqp_id', 'probe_card_id', 'chip_x_pos', 'chip_y_pos', 'subitem_id', 'temperature', 'total_site_cnt'],\
                                                columns='item_id', aggfunc='last',observed = True)

            pivoted_data = Reformatize(pivoted_data, ALIAS, FORMULA)
            pivoted_data = pivoted_data.reset_index()
            
            pivoted_data.to_parquet(GLOBAL_CONFIG.get("DB_et_LOTWF_pivot_raw") + f'pivot_raw_{current_lot}.parquet', index=False)
            print(f'{current_lot} Data 정리완료')

        print(f'@@@Inform@@@ {GLOBAL_CONFIG.get("vehicle")}__{num}/{length_of_list}일 정리 완료!!!!!!!!!!!!!!!!!!')
        
    print(f'@@@Inform@@@ {GLOBAL_CONFIG.get("vehicle")} 전체 정리 완료!!!!!!!!!!!!!!!!!!')     


def reformatter_verify(reformatter):
    
    # client = boto3.client(
    #     service_name='s3', region_name='DS',
    #     aws_access_key_id='simyung.woo',
    #     aws_secret_access_key=GLOBAL_CONFIG.get("s3_aws_secret_access_key"),
    #     endpoint_url='http://s3.api.dscloud.samsungds.net:9090')

    print("=" * 80)
    print(f"{GLOBAL_CONFIG.get('vehicle')} Reformatter Verification Start")
    print("=" * 80)
    
    Uniqueness_Verification_col = ["CATEGORY", "ITEMID", "ALIAS"]
    reformatter = reformatter.dropna(subset=Uniqueness_Verification_col, how='all')
    reformatter["Uniqueness_Verification_check"] = reformatter.apply(generate_sha256_key, axis=1, columns_to_use=Uniqueness_Verification_col)

    # CHECK 1: CATEGORY_ITEMID_ALIAS 유일성 검증
    print("\n[CHECK 1] Verifying CATEGORY_ITEMID_ALIAS uniqueness...")
    check1 = check_duplicates(reformatter, "Uniqueness_Verification_check")
    if check1:
        print(f"  ❌ FAIL: CATEGORY_ITEMID_ALIAS must be unique. Found duplicates:")
        print(f"  {check1}")
        log_to_file(f"[!!!!{GLOBAL_CONFIG.get('vehicle')} Reformatter ERROR!!!!] 유일성에 에러가 있습니다. *CATEGORY_ITEMID_ALIAS 유일하여야함.\n {check1}" , GLOBAL_CONFIG.get("running_log"))
        return False
    else:
        print(f"  ✅ PASS: All CATEGORY_ITEMID_ALIAS combinations are unique.")

    # CHECK 2: REPORT ORDER 중복 검증
    print("\n[CHECK 2] Verifying REPORT ORDER uniqueness...")
    check2 = check_duplicates(reformatter, "REPORT ORDER")
    if check2:
        print(f"  ❌ FAIL: REPORT ORDER has duplicates:")
        print(f"  {check2}")
        log_to_file(f"[!!!!{GLOBAL_CONFIG.get('vehicle')} Reformatter ERROR!!!!] Auto_REPORT 발행 항목에 중복이 있습니다.\n {check2}" , GLOBAL_CONFIG.get("running_log"))
        return False
    else:
        print(f"  ✅ PASS: All REPORT ORDER values are unique.")

    # CHECK 3: REPORT ORDER(CS) 중복 검증 (주석 처리됨)
    # print("\n[CHECK 3] Verifying REPORT ORDER(CS) uniqueness...")
    # check3 = check_duplicates(reformatter, "REPORT ORDER(CS)")
    # if check3:
    #     print(f"  ❌ FAIL: REPORT ORDER(CS) has duplicates:")
    #     print(f"  {check3}")
    #     log_to_file(f"[!!!!{GLOBAL_CONFIG.get('vehicle')} Reformatter ERROR!!!!] CS_REPORT 발행 항목에 중복이 있습니다.\n {check3}" , GLOBAL_CONFIG.get("running_log"))
    #     return False
    # else:
    #     print(f"  ✅ PASS: All REPORT ORDER(CS) values are unique.")

    # CHECK 4: REPORT ORDER 필수 컬럼 누락 검증
    print("\n[CHECK 4] Verifying required columns for REPORT ORDER...")
    print(f"  Checking columns: SPECLOW, SPECHIGH, TARGET, REPORT LOG SCALE, REPORT DIRECTION, CAT1, CAT2")
    check4 = check_non_empty(reformatter, "REPORT ORDER", ["SPECLOW","SPECHIGH","TARGET","REPORT LOG SCALE","REPORT DIRECTION","CAT1","CAT2"])
    if not check4:
        print(f"  ❌ FAIL: Missing required values for REPORT ORDER:")
        print(f"  {check4}")
        log_to_file(f"[!!!!{GLOBAL_CONFIG.get('vehicle')} Reformatter ERROR!!!!] Auto_REPORT 발행 항목에 누락설정값이 있습니다.\n {check4}" , GLOBAL_CONFIG.get("running_log"))
        return False
    else:
        print(f"  ✅ PASS: All required columns have values for REPORT ORDER rows.")

    # CHECK 5: REPORT ORDER(CS) 필수 컬럼 누락 검증 (주석 처리됨)
    # print("\n[CHECK 5] Verifying required columns for REPORT ORDER(CS)...")
    # print(f"  Checking columns: SPECLOW, SPECHIGH, TARGET, REPORT LOG SCALE, REPORT DIRECTION, CAT1(CS), CAT2(CS)")
    # check5 = check_non_empty(reformatter, "REPORT ORDER(CS)", ["SPECLOW","SPECHIGH","TARGET","REPORT LOG SCALE","REPORT DIRECTION","CAT1(CS)","CAT2(CS)"] )
    # if not check5:
    #     print(f"  ❌ FAIL: Missing required values for REPORT ORDER(CS):")
    #     print(f"  {check5}")
    #     log_to_file(f"[!!!!{GLOBAL_CONFIG.get('vehicle')} Reformatter ERROR!!!!] CS_REPORT 발행 항목에 누락설정값이 있습니다.\n {check5}" , GLOBAL_CONFIG.get("running_log"))
    #     return False
    # else:
    #     print(f"  ✅ PASS: All required columns have values for REPORT ORDER(CS) rows.")

    print("\n" + "=" * 80)
    print(f"✅ {GLOBAL_CONFIG.get('vehicle')} Reformatter ALL CHECKS PASSED")
    print("=" * 80)
    
    # save_reformatter_file_name = GLOBAL_CONFIG.get('DB')+f"{GLOBAL_CONFIG.get('vehicle')}_reformatter.csv"
    # save_reformatter_file_name_only = f"{GLOBAL_CONFIG.get('vehicle')}_reformatter.csv"
    # reformatter.to_csv(save_reformatter_file_name, index = False)
    # bucket_simyung = 'simyung.woo'
    # try :
    #     client.delete_object(Bucket=bucket_simyung, Key=f'C_DEP_Visual/{save_reformatter_file_name_only}')
    #     client.upload_file(f'{save_reformatter_file_name}', bucket_simyung, f'C_DEP_Visual/{save_reformatter_file_name_only}')
    #     print(f"기존 reformatter s3 DB에 파일 reformatter 삭제 후 upload 완료.")
    # except Exception as e:
    #     print(f"{save_reformatter_file_name}_s3 DB에 해당파일 없습니다.")
    #     client.upload_file(f'{save_reformatter_file_name}', bucket_simyung, f'C_DEP_Visual/{save_reformatter_file_name_only}')
    #     print(f"기존 reformatter s3 DB에 파일 reformatter 삭제 후 upload 완료.")

    return True   

def Reformatize(data, ALIAS, FORMULA):
    def addpf(formula,data):
        pivot = data
        def LOG(u, df):
            return np.log10(ABS(df))
        def POWER(a,b):
            return np.power(a,b)    
        def sqrt(a):
            return np.sqrt(a)
        def ABS(a):
            return np.abs(a)
        def rmax(*args):
            df_max=pd.DataFrame()
            for arg in args:
                df_max=pd.concat([df_max,arg],axis=1)
            return df_max.max(axis=1)
        def rmin(*args):
            df_min=pd.DataFrame()
            for arg in args:
                df_min=pd.concat([df_min,arg],axis=1)
            return df_min.min(axis=1)
        def MA_Window(*args):
            # set data 
            x_data,y_data = [],pd.DataFrame()
            spec,compliance = [],10
            for arg in args:
                if isinstance(arg,list):
                    x_data = np.array(arg)
                elif isinstance(arg,str) or isinstance(arg,int) or isinstance(arg,float):
                    if isinstance(spec,list):
                        spec = np.log10(float(arg))
                    else:
                        compliance = abs(float(arg))
                else:
                    y_data = pd.concat([y_data,arg.apply(lambda a: np.log10(float(a)+1E-14))],axis=1)
            # check & calculate data
            if x_data.shape[0] == y_data.shape[1]:
                # 계수계산
                df_coeffs = pd.DataFrame(np.polyfit(x_data, y_data.T, 2).T, index=y_data.index, columns=['a2', 'a1', 'a0'])
                # 판별식계산
                df_coeffs['discriminant'] = df_coeffs['a1']**2 - 4 * df_coeffs['a2'] * (df_coeffs['a0'] - spec)
                # 양방향margin계산
                df_coeffs['plus_margin'] = np.where(df_coeffs['discriminant']>=0, (-df_coeffs['a1'] + np.sqrt(df_coeffs['discriminant'])) / (2 * df_coeffs['a2']), np.nan)
                df_coeffs['minus_margin'] = np.where(df_coeffs['discriminant']>=0, (-df_coeffs['a1'] - np.sqrt(df_coeffs['discriminant'])) / (2 * df_coeffs['a2']), np.nan)
                # compliance적용
                df_coeffs['plus_margin'] = np.where(df_coeffs['plus_margin']>compliance, compliance, df_coeffs['plus_margin'])
                df_coeffs['minus_margin'] = np.where(df_coeffs['minus_margin']<-compliance, -compliance, df_coeffs['minus_margin'])
                # 볼록함수예외처리
                df_coeffs.loc[(df_coeffs['discriminant']<0)&(df_coeffs['a2']>0), ['plus_margin','minus_margin']] = 0
                # 오목함수예외처리
                df_coeffs.loc[df_coeffs['a2']<=0, ['plus_margin','minus_margin']] = [compliance,-compliance]
                # ovl_index계산
                df_coeffs['ovl_index'] = -0.5 * (df_coeffs['plus_margin'] + df_coeffs['minus_margin'])
                # ma_window계산
                df_coeffs[''] = df_coeffs['plus_margin'] - df_coeffs['minus_margin']
                # NEW ma_window계산
                df_coeffs['new'] = df_coeffs[['plus_margin', 'minus_margin']].abs().min(axis=1)
                #print("WINDOW 계산완료")
                return df_coeffs[['minus_margin','plus_margin','ovl_index','','new']]
            else:
                dummy = [np.nan for _ in range(5)]
                return pd.DataFrame([dummy for _ in range(y_data.shape[0])], index=y_data.index, columns=['minus_margin','plus_margin','ovl_index','','new'])
        def stddev(a):
            return a.groupby([pivot["root_lot_id"],pivot["wafer_id"],pivot["tkout_time"]]).transform(np.std)
        def std(a):
            return a.groupby([pivot["root_lot_id"],pivot["wafer_id"],pivot["tkout_time"]]).transform(np.std)
        def STD(a):
            return a.groupby([pivot["root_lot_id"],pivot["wafer_id"],pivot["tkout_time"]]).transform(np.std)
        def AVG(a):
            return a.groupby([pivot["root_lot_id"],pivot["wafer_id"],pivot["tkout_time"]]).transform(np.mean)
        try:
            a = eval(formula)
        except:
            a = 'error'
        return a

    #pivot 된 data에 Reformatter 적용
    ALIAS_LEFT=[]
    FORMULA_LEFT=[]
    i= 0

    for i in range(10):
        calnum = len(ALIAS) #미계산 항목 수 update
        for alias, formula in zip(ALIAS, FORMULA):
            formula_tmp = formula
            formula = formula.replace('{','(pivot.get("')
            formula = formula.replace('}','"))')
            a = addpf(formula,data)  #addp 계산 실행
            if str(type(a)) == "<class 'str'>":  #에러면 에러 항목에 넣고 아니면 계산 완료
                ALIAS_LEFT.append(alias)
                FORMULA_LEFT.append(formula_tmp)
                continue
            else:
                if isinstance(a,pd.DataFrame) and a.shape[1] > 1:
                    data[[alias+'_'+col if col!='' else alias for col in a]] = a
                else:
                    data[alias] = a
        ALIAS = ALIAS_LEFT
        FORMULA = FORMULA_LEFT
        ALIAS_LEFT = []
        FORMULA_LEFT = []

        if calnum==len(ALIAS):  #모든 항목이 계산되었는지 확인
            break
        else:
            continue

    return data

