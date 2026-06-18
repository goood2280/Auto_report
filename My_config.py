import yaml  
from dotenv import load_dotenv  
import os

class Config:  
    def __init__(self):  
        load_dotenv()

        self.suffixes_remove = ["SUFFIX_01", "SUFFIX_02", "SUFFIX_03", "SUFFIX_04", "SUFFIX_05"]  
        self.replace_map = {  
                        "CODE_A": "VALUE_A",  
                        "CODE_B": "VALUE_B",  
                        "CODE_C": "VALUE_C",  
                        "CODE_D": "VALUE_D",  
                        "CODE_E": "VALUE_E",  
                        "CODE_F": "VALUE_F",  
                        "CODE_G": "VALUE_G",  
                        "CODE_H": "VALUE_H",  
                        "CODE_I": "VALUE_I",  
                        "CODE_J": "VALUE_J",  
                        "CODE_K": "VALUE_K",  
                        "CODE_L": "VALUE_L",  
                        "CODE_M": "VALUE_M",  
                        "CODE_N": "VALUE_N",  
                        "CODE_O": "VALUE_O",  
                        "CODE_P": "VALUE_P",  
                        "CODE_Q": "VALUE_Q",  
                        "CODE_R": "VALUE_R",  
                        "CODE_S": "VALUE_S",  
                        "CODE_T": "VALUE_T",  
                        "CODE_U": "VALUE_U",  
                        "CODE_V": "VALUE_V",  
                        "CODE_W": "VALUE_W",  
                        "CODE_X": "VALUE_X",  
                        "CODE_Y": "VALUE_Y",  
                        "CODE_Z": "VALUE_Z",  
                    }

        self.et_custom_columns = ['fab_lot_id','lot_id', 'root_lot_id', 'wafer_id', 'process_id', 'part_id','step_id', 'step_seq', 'tkout_time', \  
                                'item_id', 'flat_zone', 'eqp_id', 'probe_card_id','chip_x_pos', 'chip_y_pos','subitem_id','et_value','temperature','total_site_cnt']                    
        self.wip_custom_columns = ['line_id', 'lot_id', 'step_seq','lot_current_loc','last_update_date']  
        self.fab_custom_columns = ['root_lot_id','wafer_id','ppid','eqp_id','tkout_time','chamber_id','reticle_id']  
        self.inline_custom_columns = ['root_lot_id', 'wafer_id', 'tkout_time', 'step_id', 'item_id', 'fab_value','spc_ctrl_spec_high','spc_ctrl_spec_limit','spc_ctrl_spec_low']    
          
        self.et_file_path = 'PATH_TO_FILE_01'  
        self.fab_file_path = 'PATH_TO_FILE_02'  
        self.inline_file_path = 'PATH_TO_FILE_03'  
        self.coordinate_file_path = 'PATH_TO_FILE_04'  
        self.template_ppt_path = 'PATH_TO_FILE_05'  
        self.description_ppt_path = 'PATH_TO_FILE_06'  
        self.email_list_path = 'PATH_TO_FILE_07'

        self.dc_dict = {  
            'ID_01': 'LOC_01', 'ID_02': 'LOC_02', 'ID_03': 'LOC_03', 'ID_04': 'LOC_04', 'ID_05': 'LOC_05',  
            'NON' : "LOC_DEFAULT", 'A00000': 'LOC_DEFAULT', 'Scrap': 'LOC_DEFAULT'  
        }  
              
        self.mask_table_sheet = 'SHEET_01'  
        self.inline_file_sheet = 'SHEET_02'  
        self.teg_description_dict = {  
            'ITEM_01': 'URL_LINK_01',  
            'ITEM_02': 'URL_LINK_02',  
            'ITEM_03': 'URL_LINK_03'  
        }

        self.env = {}  
        self.settings = {}    
        self.generated_vars = {}  

        self._load_env_variables()  
      
    def _load_env_variables(self):  
        load_dotenv()  
        for key, value in os.environ.items():  
            self.env[key] = value

    def load_from_yaml(self, item_name, yaml_path="config.yaml"):  
        with open(yaml_path, "r") as file:  
            config_data = yaml.safe_load(file)

        if item_name not in config_data:  
            raise ValueError(f"{item_name} 항목이 config.yaml에 존재하지 않습니다.")

        self.settings = config_data[item_name]  
        self._generate_dependent_vars()

    def _generate_dependent_vars(self):  
        self.generated_vars["url"] = f"https://api.internal.system/send?systemId={self.settings['YOUR_PROJECT']}&user={self.settings['KNOXID']}"  
        self.generated_vars["html_code"] = """  
                    <!DOCTYPE html>  
                    <html>  
                    <head>  
                        <style>  
                            td:first-child, th:first-child { border-left: none; }  
                            thead { position: sticky; top: 0; }  
                            table, th, td {  
                                width: Auto; border-collapse: collapse; margin: 0;  
                                font-family: Arial, sans-serif; padding: 1px 3px;  
                                border: 1px solid black; text-align: center; font-size: 12px;  
                            }  
                            th { background-color: #f0f0f0; }  
                            body { overflow: auto; }  
                        </style>  
                    </head>  
                    """  
        self.generated_vars["html_code_content"] = f"""  
                    <body>  
                        <div id="top"></div>  
                        <strong style="font-size:22px"> [{self.settings['node']}] {self.settings['vehicle']} - Analysis Report </strong> <br>  
                        <strong style="font-size:14px">&nbsp; Automated system providing processed data and charts </strong> <br>  
                        <br>  
                        <strong style="font-size:16px"> ■ Notice </strong> <br>  
                        <strong style="font-size:12px"> &nbsp;▷ This is an automated reporting system for {self.settings['node']} {self.settings['vehicle']}. </strong> <br>  
                        <strong style="font-size:12px"> &nbsp;▷ Update &nbsp;-&nbsp;<a href="INTERNAL_LINK_01"> {self.settings['version']} </a> </strong> <br>  
                        <strong style="font-size:12px"> &nbsp;▷ Description &nbsp;-&nbsp;<a href="{self.teg_description_dict.get(self.settings['vehicle'], '#')}"> {self.settings['vehicle']} </a> </strong> <br>  
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;▷ Mail filter settings: </strong> <br>  
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;&nbsp;&nbsp;(1) Sender : ADMIN_USER </strong> <br>  
                        <strong style="color:mediumblue; font-size:12px"> &nbsp;&nbsp;&nbsp;(2) Subject : [AUTO REPORT] </strong> <br>  
                        <br>  
                        <strong style="font-size:16px"> ■ Inquiry </strong> <br>  
                        <strong style="font-size:12px"> &nbsp;▷ Admin : {self.settings['system_admin']} </strong> <br>  
                        <strong style="font-size: 12px;"> &nbsp;▷ Archive : <a href="INTERNAL_LINK_02"> System Link </a></strong><br>  
                        <br>  
                        <strong style="font-size:16px"> ■ Contents </strong><br>  
                        <strong style="font-size:12px"> &nbsp; <a href="#target1" style="font-size: 13px;">[1] Summary Board</a> </strong><br>  
                        <strong style="font-size:12px"> &nbsp; <a href="#target3" style="font-size: 13px;">[2] Data Table</a> </strong><br>  
                        <strong style="font-size:12px"> &nbsp; <a href="#target4" style="font-size: 13px;">[3] Detailed Analysis</a> </strong><br>  
                        <br>  
                        <strong style="color:lightgray">--------------------------------------------------<br></strong>  
                    </body>  
                    </html>  
                """  
        self.generated_vars["html_code"] = self.generated_vars["html_code"] + self.generated_vars["html_code_content"]  

        self.generated_vars["ROOT"] = 'BASE_DIR/'  
        self.generated_vars["DB"] = 'BASE_DIR/DB/'  
        self.generated_vars["DB_et_daily"] = self.generated_vars["DB"] + self.settings['vehicle'] + '_daily/'  
        self.generated_vars["DB_et_LOTWF_raw"] = self.generated_vars["DB"] + self.settings['vehicle'] + '_RAW/'  
        self.generated_vars["DB_et_LOTWF_pivot_raw"]  = self.generated_vars["DB"] + self.settings['vehicle'] + '_RAW/pivot/'

        self.generated_vars["Report"] = 'BASE_DIR/Report/'  
        self.generated_vars["low_qual_ppt_save_path"] = f'BASE_DIR/Report/{self.settings["vehicle"]}/Mail/'  
        self.generated_vars["low_qual_ppt_save_path_cs"] = f'BASE_DIR/Report/{self.settings["vehicle"]}/Mail/'  
        self.generated_vars["high_qual_ppt_save_path"] = f'BASE_DIR/Report/{self.settings["vehicle"]}/EDM/'  
        self.generated_vars["html_save_path"] = f'BASE_DIR/Report/{self.settings["vehicle"]}/HTML/'

        self.generated_vars["log"] = 'BASE_DIR/log/'  
        self.generated_vars["query_log"] = 'BASE_DIR/log/' + self.settings['vehicle'] + '_query_log.txt'  
        self.generated_vars["loop_log"] = 'BASE_DIR/log/' + self.settings['vehicle'] + '_loop_log.txt'  
        self.generated_vars["error_log"] = 'BASE_DIR/log/' + self.settings['vehicle'] + '_error_log.txt'  
        self.generated_vars["et_log_path"] = 'BASE_DIR/log/' + self.settings['vehicle'] + '_log.csv'  
        self.generated_vars["Final_et_log_path"] = 'BASE_DIR/log/' + self.settings['vehicle'] + '_log_Final.csv'  
        self.generated_vars["running_log"] = 'BASE_DIR/log/running_log.txt'

    def get(self, key, default=None):  
        if key in self.settings:  
            return self.settings[key]  
        if key in self.generated_vars:  
            return self.generated_vars[key]  
        if key in self.env :  
            return self.env[key]  
        if hasattr(self, key):  
            return getattr(self, key)  
        return default

GLOBAL_CONFIG = Config()  
