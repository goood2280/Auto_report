import yaml
from dotenv import load_dotenv
import os

class Config:
    """설정을 관리하는 클래스"""
    def __init__(self):

        """.env 파일에서 설정 로드"""
        load_dotenv()  # .env 파일 읽기

        self.endpoint_url = '0' 
        self.GPT_USER_ID = 'simyung.woo'
        self.bucket_dx = 'simyung.woo'
        self.bucket_simyung = 'simyung.woo'

        self.suffixes_remove = ["ZZ", "M1X1", "_P1", "_P2", "tot"]
        self.replace_map = {
                        "AAA":"AA_Rs"
                    }

        self.et_custom_columns = ['fab_lot_id','lot_id', 'root_lot_id', 'wafer_id', 'process_id', 'part_id','step_id', 'step_seq', 'tkout_time', \
                                'item_id', 'flat_zone', 'eqp_id', 'probe_card_id','chip_x_pos', 'chip_y_pos','subitem_id','et_value','temperature','total_site_cnt']                  
        self.wip_custom_columns = ['line_id', 'lot_id', 'step_seq','lot_current_loc','last_update_date']
        self.fab_custom_columns = ['root_lot_id','wafer_id','ppid','eqp_id','tkout_time','chamber_id','reticle_id']
        self.inline_custom_columns = ['root_lot_id', 'wafer_id', 'tkout_time', 'step_id', 'item_id', 'fab_value','spc_ctrl_spec_high','spc_ctrl_spec_limit','spc_ctrl_spec_low']  
        
        # 경로 설정: Main.py가 있는 디렉토리 기준
        self.base_path = os.path.dirname(os.path.abspath(__file__))
        self.et_file_path = os.path.join(self.base_path, 'HOL_reformatter.xlsx')
        self.fab_file_path = os.path.join(self.base_path, 'SF3_Data_Extractor_Input_File_v0.xlsx')
        self.inline_file_path = os.path.join(self.base_path, 'INLINE_1_reformatter.xlsx')
        self.coordinate_file_path = os.path.join(self.base_path, 'SF3_Data_Extractor_Input_File_v0.xlsx')
        self.template_ppt_path = os.path.join(self.base_path, 'HOL_Auto_Report_Template.pptx')
        self.description_ppt_path = os.path.join(self.base_path, 'HOL_Auto_Report_Description.pptx')
        self.email_list_path = os.path.join(self.base_path, 'HOL_Auto_Report_Mailing_List.xlsx')

        # Grouped structure: DC step -> [step_ids] for easier management
        self.dc_step_to_ids = {
            'MFDC': ['test'],
        }
        
        # Backward compatibility: step_id -> DC step (original structure)
        self.dc_dict = {step_id: dc_step 
                       for dc_step, step_ids in self.dc_step_to_ids.items() 
                       for step_id in step_ids}
            
        self.mask_table_sheet = 'MASK_TABLE'
        self.inline_file_sheet = 'INLINE_1'

        self.env = {}
        self.settings = {}  # YAML에서 로드된 원본 설정
        self.generated_vars = {}  # 종속 변수 저장

        # .env 파일 읽기 및 초기화
        self._load_env_variables()
    
    def _load_env_variables(self):
        """환경 변수를 읽어서 self.env에 저장"""
        load_dotenv()  # .env 파일 로드
        for key, value in os.environ.items():
            self.env[key] = value  # 모든 키-값 저장

    def load_from_yaml(self, item_name, yaml_path="reformatter/config.yaml"):
        """config.yaml 에서 특정 항목 로드 및 변수 생성"""
        with open(yaml_path, "r", encoding='utf-8') as file:
            config_data = yaml.safe_load(file)

        if item_name not in config_data:
            raise ValueError(f"{item_name} 항목이 config.yaml에 존재하지 않습니다.")

        # 원본 설정 저장
        self.settings = config_data[item_name]

        # 종속 변수 생성
        self._generate_dependent_vars()

    def _generate_dependent_vars(self):
        """로드된 설정을 기반으로 종속 변수 생성"""
        # 명시적으로 종속 변수 생성
        self.generated_vars["url"] = f"http://catalog.itplatform.sec.samsung.net:7979/apim/mail/3/api/v1/shared/mail/mails2/send/attach?systemId={self.settings['YOUR_PROJECT']}&loginUser.login={self.settings['KNOXID']}"
        self.generated_vars["html_code"] = """
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <style>
                            td:first-child, th:first-child {
                                border-left: none;
                                width: 60px !important;
                                min-width: 60px !important;
                                max-width: 60px !important;
                            }
                            thead {
                                position: sticky;
                                top: 0;
                            }
                            table {
                                table-layout: fixed;
                                width: 100%;
                                border-collapse: collapse;
                                margin: 0;
                                font-family: Arial, sans-serif;
                                padding: 1px 3px;
                                border: 1px solid black;
                                text-align: center;
                                font-size: 12px;
                            }
                            th, td {
                                overflow: hidden;
                                text-overflow: ellipsis;
                                white-space: nowrap;
                                border-right: 1px solid black;
                                border-bottom: 1px solid black;
                            }
                            th:not(:first-child), td:not(:first-child) {
                                padding: 1px 2px;
                            }
                            th {
                                background-color: #f0f0f0;
                            }
                            body {
                                overflow: auto;
                            }
                            /* Table scrollable container - FIXED SIZE TABLE */
                            .table-container {
                                overflow-x: auto !important;
                                overflow-y: visible !important;
                                width: 100% !important;
                                max-width: 100% !important;
                                border: 1px solid black;
                                display: block !important;
                                position: relative !important;
                            }

                            /* Score Board - Index columns auto-width, wafer ID columns fixed width */
                            table.score-board {
                                table-layout: auto !important;
                                width: auto !important;
                                display: inline-table !important;
                                border-collapse: collapse !important;
                            }

                            /* Score Board - prevent text truncation on ALL cells (index + columns) */
                            table.score-board th,
                            table.score-board td {
                                overflow: visible !important;
                                text-overflow: clip !important;
                                white-space: nowrap !important;
                                width: auto !important;
                                min-width: auto !important;
                                max-width: none !important;
                                padding: 4px 8px !important;
                            }

                            /* Score Board first column (row labels) - auto width to show full item names, override base 60px */
                            table.score-board th:first-child,
                            table.score-board td:first-child,
                            table.score-board tbody th:first-child {
                                width: auto !important;
                                min-width: 150px !important;
                                max-width: none !important;
                                overflow: visible !important;
                                white-space: normal !important;
                                word-wrap: break-word !important;
                                text-align: left !important;
                                padding: 4px 8px !important;
                            }

                            /* Score Board second column (label) - auto width */
                            table.score-board th:nth-child(2),
                            table.score-board td:nth-child(2),
                            table.score-board tbody th:nth-child(2) {
                                width: auto !important;
                                min-width: 250px !important;
                                max-width: none !important;
                                overflow: visible !important;
                                white-space: normal !important;
                                word-wrap: break-word !important;
                                text-align: left !important;
                                padding: 4px 8px !important;
                            }

                            /* Score Board data columns (wafer ID columns #1, #2, etc.) - FIXED width */
                            table.score-board td:nth-child(n+3),
                            table.score-board th:nth-child(n+3) {
                                width: 55px !important;
                                min-width: 55px !important;
                                max-width: 55px !important;
                                overflow: visible !important;
                                white-space: nowrap !important;
                                padding: 4px 8px !important;
                            }

                            /* Score Board header - display full text without truncation 
                               (wafer ID columns keep their fixed 55px width from nth-child(n+3) rule) */
                            table.score-board thead th {
                                overflow: visible !important;
                                white-space: nowrap !important;
                                min-width: auto !important;
                                max-width: none !important;
                                text-overflow: clip !important;
                                padding: 4px 8px !important;
                            }

                            /* Inline Table - FIXED table size */
                            table.inline-table {
                                table-layout: fixed !important;
                                width: fit-content !important;
                                min-width: fit-content !important;
                                display: inline-table !important;
                                border-collapse: collapse !important;
                            }

                            /* Inline Table column widths */
                            table.inline-table th:nth-of-type(1),
                            table.inline-table td:nth-of-type(1) {
                                width: 40px !important;
                                min-width: 40px !important;
                                max-width: 40px !important;
                            }

                            table.inline-table th:nth-of-type(2),
                            table.inline-table td:nth-of-type(2) {
                                width: 350px !important;
                                min-width: 350px !important;
                                max-width: 350px !important;
                            }

                            table.inline-table th:nth-of-type(3),
                            table.inline-table td:nth-of-type(3) {
                                width: 110px !important;
                                min-width: 110px !important;
                                max-width: 110px !important;
                            }

                            table.inline-table th:nth-of-type(n+4),
                            table.inline-table td:nth-of-type(n+4) {
                                width: 45px !important;
                                min-width: 45px !important;
                                max-width: 45px !important;
                            }

                            /* Lot Detail Table - FIXED table size */
                            table.lot-detail-table {
                                table-layout: fixed !important;
                                width: fit-content !important;
                                min-width: fit-content !important;
                                display: inline-table !important;
                                border-collapse: collapse !important;
                            }

                            table.lot-detail-table th,
                            table.lot-detail-table td {
                                padding: 2px 4px !important;
                                overflow: hidden !important;
                                text-overflow: ellipsis !important;
                                white-space: nowrap !important;
                                border: 1px solid black !important;
                            }

                            /* Lot Detail column widths */
                            table.lot-detail-table th:nth-child(1),
                            table.lot-detail-table td:nth-child(1) {
                                width: 120px !important;
                                min-width: 120px !important;
                                max-width: 120px !important;
                            }

                            table.lot-detail-table th:nth-child(2),
                            table.lot-detail-table td:nth-child(2) {
                                width: 150px !important;
                                min-width: 150px !important;
                                max-width: 150px !important;
                            }

                            table.lot-detail-table th:nth-child(3),
                            table.lot-detail-table td:nth-child(3) {
                                width: 100px !important;
                                min-width: 100px !important;
                                max-width: 100px !important;
                            }

                            table.lot-detail-table th:nth-child(4),
                            table.lot-detail-table td:nth-child(4) {
                                width: 120px !important;
                                min-width: 120px !important;
                                max-width: 120px !important;
                            }

                            table.lot-detail-table th:nth-child(5),
                            table.lot-detail-table td:nth-child(5) {
                                width: 350px !important;
                                min-width: 350px !important;
                                max-width: 350px !important;
                            }

                            table.lot-detail-table th:nth-child(6),
                            table.lot-detail-table td:nth-child(6) {
                                width: 180px !important;
                                min-width: 180px !important;
                                max-width: 180px !important;
                            }
                        </style>
                    </head>
                    """
        self.generated_vars["html_code_content"] = f"""
                    <body>
                        <div id="top"></div>
                        <strong style="font-size:22px"> [{self.settings['node']} HOL] {self.settings['vehicle']} sub_title </strong> <br>
                        <strong style="font-size:14px">&nbsp; Python-based automated system providing new DC results, charts </strong> <br>
                        <br>
                        <strong style="font-size:16px"> ■ 공지 사항 </strong> <br>
                        <strong style="font-size:12px"> &nbsp;▷ Python 기반의 새로운 {self.settings['node']} {self.settings['vehicle']} HOL DC 측정 결과 자동 메일 발송 시스템 입니다. </strong> <br>
                        
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;▷ 메일 자동 분류는 아래 조건으로 설정하면 됩니다: </strong> <br>
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;&nbsp;&nbsp;(1) 보낸 사람 : 우시명 </strong> <br>
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;&nbsp;&nbsp;(2) 메일 제목 : 다음 키워드를 포함할 때 - [HOL AUTO REPORT] </strong> <br>
                        <br>
                        <strong style="font-size:16px"> ■ 문의 및 요청사항</strong> <br>
                        <strong style="font-size:12px"> &nbsp;▷ 수신처 추가 및 시스템 문의 : {self.settings['system_admin']} </strong> <br>
                        <strong style="font-size: 12px;"> &nbsp;▷ Lot Report Archive : <a href= https://go/pa-web > DX Web System Link </a></strong><br>
                        <br>
                        <strong style="font-size:16px"> ■ Contents </strong><br>
                        <strong style="font-size:12px"> &nbsp; <a href="#target0" style="font-size: 13px;">[0] 이상차트 (Trend Chart)</a> </strong><br>
                        <strong style="font-size:12px"> &nbsp; <a href="#target1" style="font-size: 13px;">[1] 이상요약 (Anomaly Summary)</a> </strong><br>
                        <strong style="font-size:12px"> &nbsp; <a href="#target2" style="font-size: 13px;">[2] Score Board</a> </strong><br>
                        <strong style="font-size:12px"> &nbsp; <a href="#target3" style="font-size: 13px;">[3] Inline Table</a> </strong><br>
                        <strong style="font-size:12px"> &nbsp; <a href="#target4" style="font-size: 13px;">[4] 최근 DC측정자재 상세</a> </strong><br>
                        <br>
                        <strong style="color:lightgray">--------------------------------------------------<br></strong>
                        <!-- Section placeholders for dynamic content insertion -->
                        <div id="target0"></div>
                        <div id="target1"></div>
                        <div id="target2"></div>
                        <div id="target3"></div>
                        <div id="target4"></div>
                    </body>
                    </html>
                """
        self.generated_vars["html_code"] =  self.generated_vars["html_code"] + self.generated_vars["html_code_content"]  

        # ======================================================= DB Path ===============================================================

        self.generated_vars["ROOT"] = os.path.join(self.base_path, 'RUN') + os.sep
        self.generated_vars["DB"] = os.path.join(self.base_path, 'RUN', 'DB') + os.sep
        self.generated_vars["DB_et_daily"] = os.path.join(self.base_path, 'RUN', 'DB', self.settings['vehicle'] + '_daily') + os.sep
        self.generated_vars["DB_et_LOTWF_raw"] = os.path.join(self.base_path, 'RUN', 'DB', self.settings['vehicle'] + '_LOTWF') + os.sep
        self.generated_vars["DB_et_LOTWF_pivot_raw"]  = os.path.join(self.base_path, 'RUN', 'DB', self.settings['vehicle'] + '_LOTWF', 'pivot_raw') + os.sep

        # ======================================================= Report Path ===========================================================

        self.generated_vars["Report"] = os.path.join(self.base_path, 'RUN', 'Report') + os.sep
        self.generated_vars["low_qual_ppt_save_path"] = os.path.join(self.base_path, 'RUN', 'Report', self.settings["vehicle"], 'Mail') + os.sep
        self.generated_vars["low_qual_ppt_save_path_cs"] = os.path.join(self.base_path, 'RUN', 'Report', self.settings["vehicle"], 'Mail') + os.sep
        self.generated_vars["high_qual_ppt_save_path"] = os.path.join(self.base_path, 'RUN', 'Report', self.settings["vehicle"], 'EDM') + os.sep
        self.generated_vars["html_save_path"] = os.path.join(self.base_path, 'RUN', 'Report', self.settings["vehicle"], 'HTML') + os.sep

        # ======================================================= Log Path ==============================================================

        self.generated_vars["log"] = os.path.join(self.base_path, 'RUN', 'log') + os.sep
        self.generated_vars["query_log"] = os.path.join(self.base_path, 'RUN', 'log', self.settings['vehicle'] + '_query_log.txt')
        self.generated_vars["loop_log"] = os.path.join(self.base_path, 'RUN', 'log', self.settings['vehicle'] + '_loop_log.txt')
        self.generated_vars["error_log"] = os.path.join(self.base_path, 'RUN', 'log', self.settings['vehicle'] + '_error_log.txt')
        self.generated_vars["et_log_path"] = os.path.join(self.base_path, 'RUN', 'log', self.settings['vehicle'] + '_et_log.csv')
        self.generated_vars["Final_et_log_path"] = os.path.join(self.base_path, 'RUN', 'log', self.settings['vehicle'] + '_et_log_Final.csv')
        self.generated_vars["running_log"] = os.path.join(self.base_path, 'RUN', 'log', 'running_log.txt')

    def get_dc_step_from_id(self, step_id, default=None):
        """Get DC step from step_id using dc_dict lookup.
        
        Args:
            step_id: The step ID to look up (e.g., 'NU467300')
            default: Default value if step_id not found
            
        Returns:
            DC step value (e.g., 'M1DC') or default if not found
        """
        return self.dc_dict.get(step_id, default)
    
    def get_step_ids_from_dc_step(self, dc_step, default=None):
        """Get list of step_ids for a given DC step from grouped structure.
        
        Args:
            dc_step: The DC step to look up (e.g., 'M1DC')
            default: Default value if dc_step not found
            
        Returns:
            List of step IDs or default if not found
        """
        return self.dc_step_to_ids.get(dc_step, default)
    
    def get(self, key, default=None):
        """원본 설정, 종속 변수, 기본 변수에서 값 가져오기"""
        # 원본 설정에서 검색
        if key in self.settings:
            return self.settings[key]
        # 종속 변수에서 검색
        if key in self.generated_vars:
            return self.generated_vars[key]
        if key in self.env :
            return self.env[key]
        # 기본 변수에서 검색
        if hasattr(self, key):
            return getattr(self, key)
        # 기본값 반환
        return default

# 글로벌 설정 객체
GLOBAL_CONFIG = Config()



