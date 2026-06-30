# ============================================================
# My_config.py  –  Auto Report 전역 설정 관리 모듈
# (Global configuration management module for Auto Report)
# ============================================================
# 이 모듈은 프로젝트 전체에서 공유되는 설정(Config)을 관리합니다.
# Vehicle별 개별 설정은 reformatter/config.yaml 에서 로드합니다.
#
# 사용법 (Usage):
#   from My_config import GLOBAL_CONFIG
#   GLOBAL_CONFIG.load_from_yaml('vehicle_A')
#   value = GLOBAL_CONFIG.get('some_key')
# ============================================================

import yaml
from dotenv import load_dotenv
import os


class Config:
    """Auto Report 설정 관리 클래스 (Configuration manager class).

    전역 설정(global-only)과 vehicle별 설정(per-vehicle)을 분리하여 관리합니다.
    - 전역 설정: __init__에서 초기화 (모든 vehicle에 공통)
    - Vehicle별 설정: load_from_yaml()으로 config.yaml에서 로드
    - 종속 변수: _generate_dependent_vars()에서 자동 생성 (경로, HTML 등)
    """

    def __init__(self):
        """Config 인스턴스를 초기화합니다 (Initialize Config instance).

        Global-only 설정값을 정의하고, .env 환경 변수를 로드합니다.
        Vehicle별 설정은 load_from_yaml() 호출 시 로드됩니다.
        """

        # ──────────────────────────────────────────────────────
        # .env 환경 변수 로드 (Load environment variables)
        # ──────────────────────────────────────────────────────
        load_dotenv()

        # ──────────────────────────────────────────────────────
        # S3 / API 엔드포인트 설정 (S3 / API endpoint settings)
        # ──────────────────────────────────────────────────────
        self.endpoint_url = '0'                    # S3 호환 엔드포인트 URL
        self.GPT_USER_ID = 'simyung.woo'           # GPT API 사용자 ID
        self.bucket_dx = 'simyung.woo'             # DX 용 S3 버킷 이름
        self.bucket_simyung = 'simyung.woo'        # 개인 S3 버킷 이름
        self.s3_region_name = 'DS'                 # S3 리전 이름 (region name)

        # ──────────────────────────────────────────────────────
        # 데이터 전처리 설정 (Data preprocessing settings)
        # ──────────────────────────────────────────────────────
        # suffixes_remove: step_id 끝에서 제거할 접미사 목록
        self.suffixes_remove = ["ZZ", "M1X1", "_P1", "_P2", "tot"]

        # replace_map: item_id 등에서 치환할 문자열 매핑
        self.replace_map = {
            "AAA": "AA_Rs"
        }

        # ──────────────────────────────────────────────────────
        # DB 쿼리 컬럼 정의 (DB query column definitions)
        # 각 데이터 소스별로 가져올 컬럼 목록을 정의합니다.
        # ──────────────────────────────────────────────────────
        # ET (Electrical Test) 데이터 컬럼
        self.et_custom_columns = [
            'fab_lot_id', 'lot_id', 'root_lot_id', 'wafer_id',
            'process_id', 'part_id', 'step_id', 'step_seq',
            'tkout_time', 'item_id', 'flat_zone', 'eqp_id',
            'probe_card_id', 'chip_x_pos', 'chip_y_pos',
            'subitem_id', 'et_value', 'temperature', 'total_site_cnt'
        ]

        # WIP (Work-In-Process) 데이터 컬럼
        self.wip_custom_columns = [
            'line_id', 'lot_id', 'step_seq',
            'lot_current_loc', 'last_update_date'
        ]

        # FAB (Fabrication) 공정 데이터 컬럼
        self.fab_custom_columns = [
            'root_lot_id', 'wafer_id', 'ppid', 'eqp_id',
            'tkout_time', 'chamber_id', 'reticle_id'
        ]

        # Inline 계측 데이터 컬럼
        self.inline_custom_columns = [
            'root_lot_id', 'wafer_id', 'tkout_time', 'step_id',
            'item_id', 'fab_value', 'spc_ctrl_spec_high',
            'spc_ctrl_spec_limit', 'spc_ctrl_spec_low'
        ]

        # ──────────────────────────────────────────────────────
        # 파일 경로 설정 (File path settings)
        # base_path 기준으로 모든 입력 파일 경로를 정의합니다.
        # ──────────────────────────────────────────────────────
        self.base_path = os.path.dirname(os.path.abspath(__file__))

        # ET reformatter 엑셀 파일 경로
        self.et_file_path = os.path.join(
            self.base_path, 'HOL_reformatter.xlsx')
        # FAB 데이터 추출 입력 파일 경로
        self.fab_file_path = os.path.join(
            self.base_path, 'SF3_Data_Extractor_Input_File_v0.xlsx')
        # Inline reformatter 엑셀 파일 경로
        self.inline_file_path = os.path.join(
            self.base_path, 'INLINE_1_reformatter.xlsx')
        # 좌표 데이터 파일 경로 (FAB 파일과 동일)
        self.coordinate_file_path = os.path.join(
            self.base_path, 'SF3_Data_Extractor_Input_File_v0.xlsx')
        # PPT 템플릿 파일 경로
        self.template_ppt_path = os.path.join(
            self.base_path, 'HOL_Auto_Report_Template.pptx')
        # PPT 설명 문서 경로
        self.description_ppt_path = os.path.join(
            self.base_path, 'HOL_Auto_Report_Description.pptx')
        # 메일링 리스트 엑셀 파일 경로
        self.email_list_path = os.path.join(
            self.base_path, 'HOL_Auto_Report_Mailing_List.xlsx')

        # ──────────────────────────────────────────────────────
        # 시트 이름 설정 (Sheet name settings)
        # ──────────────────────────────────────────────────────
        self.mask_table_sheet = 'MASK_TABLE'    # 마스크 테이블 시트명
        self.inline_file_sheet = 'INLINE_1'     # Inline 데이터 시트명

        # ──────────────────────────────────────────────────────
        # Score Board 설정 (Score board thresholds & colors)
        # 점수 구간별 배경색/글자색을 정의합니다.
        # score_thresholds: 내림차순 점수 경계값 리스트
        # score_colors: 경계값별 색상 매핑 (bg=배경, fg=글자)
        # ──────────────────────────────────────────────────────
        self.score_thresholds = [100.0, 90.0, 70.0, 50.0]

        self.score_colors = {
            100: {'bg': '#00B050', 'fg': '#ffffff'},   # 100점: 초록 (green)
            90:  {'bg': '#92D050', 'fg': '#000000'},   #  90+: 연초록 (light green)
            70:  {'bg': '#FFC000', 'fg': '#000000'},   #  70+: 주황/노랑 (amber)
            50:  {'bg': '#FF0000', 'fg': '#ffffff'},   #  50+: 빨강 (red)
            0:   {'bg': '#C00000', 'fg': '#ffffff'},   #  <50: 진빨강 (dark red)
            'na': {'bg': '#555555', 'fg': '#ffffff'},  # N/A: 회색 (gray)
        }

        # ──────────────────────────────────────────────────────
        # 리포트 디자인 테마 설정 (Report Design Theme Settings)
        # 엔지니어가 PPT 및 차트의 색상, 폰트, 여백 등을 쉽게 변경할 수 있습니다.
        # ──────────────────────────────────────────────────────
        self.theme_font_family = 'Segoe UI'        # 기본 폰트
        self.theme_header_bg = (31, 73, 125)       # 테이블 헤더 배경색 (R, G, B) - Dark Blue
        self.theme_header_fg = (255, 255, 255)     # 테이블 헤더 글자색 (R, G, B) - White
        self.theme_zebra_bg1 = (242, 242, 242)     # 행 엇갈림 배경 1 (Light Gray)
        self.theme_zebra_bg2 = (255, 255, 255)     # 행 엇갈림 배경 2 (White)
        self.theme_table_border = (200, 200, 200)  # 테이블 테두리 색상 (Light Gray)
        self.theme_title_color = (31, 73, 125)     # 슬라이드/차트 제목·헤더 색상 (Dark Blue, 전 슬라이드 통일)

        self.plot_style = 'seaborn-v0_8-whitegrid' # Matplotlib 전역 스타일
        self.plot_cmap = 'viridis'                 # WF MAP 컬러맵
        self.plot_wafer_colors = 'tab20'           # Wafer 구분을 위한 컬러 팔레트

        # ──────────────────────────────────────────────────────
        # PPT 차트 렌더링 설정 (Chart rendering settings for PPT)
        # 엔지니어가 PPT에 삽입되는 차트의 해상도/압축 품질을 직접 조정합니다.
        # 화질↑ 시 용량도 함께 증가하므로 PPT 10MB 한도(목표 5~7MB)를 고려해 조정하세요.
        # ──────────────────────────────────────────────────────
        # 용량 가드: 최종 PPT가 10MB를 넘으면 아래 순서로 낮추세요
        #   1) ppt_chart_jpg_quality(라인 차트 = 용량의 대부분)  2) ppt_map_jpg_quality  3) ppt_chart_dpi
        # 측정 기준(합성 25웨이퍼·2서브아이템): 슬라이드당 약 150KB → 항목 수에 비례해 증가.
        self.ppt_chart_dpi = 150           # PPT 삽입 차트 해상도 (DPI). 높이면 선명+용량↑
        self.ppt_chart_jpg_quality = 70    # 라인/박스/트렌드/CDF/레전드 JPEG 품질 (1~95, 용량의 주 레버)
        self.ppt_map_jpg_quality = 60      # WF MAP(연속색 산점) JPEG 품질

        # ──────────────────────────────────────────────────────
        # 차트 공통 색 팔레트 (Chart color palette, matplotlib hex)
        # 차트 내부 요소(제목/축/보조선/그리드)의 색을 한 곳에서 통일합니다.
        # ──────────────────────────────────────────────────────
        self.chart_navy = '#1F497D'        # 제목/축 라벨 (theme_title_color와 동일 색)
        self.chart_accent = '#EF4444'      # target lot / spec line / outlier (레드)
        self.chart_neutral = '#475569'     # 보조선/중앙값 (슬레이트)
        self.chart_grid = '#EAECEF'        # 그리드 색
        self.chart_spine = '#CBD5E1'       # 축선(스파인) 색

        # Trend 비교 색상 (vehicle vs with_vehicle vs target lot)
        self.chart_vehicle = '#22C55E'     # main vehicle scatter (초록)
        self.chart_with_vehicle = '#9CA3AF'  # with_vehicle(동반) scatter (회색)
        self.chart_band = '#BBF7D0'        # vehicle 기준 1~99% 구름대 (연초록)

        # ──────────────────────────────────────────────────────
        # 이미지 품질 설정 (Image quality settings)
        # PPT 삽입 시 저화질/고화질 이미지 품질을 결정합니다.
        # ──────────────────────────────────────────────────────
        self.img_quality_low = 12          # 메일용 저화질 (low quality for mail)
        self.img_quality_high = 95         # EDM용 고화질 (high quality for EDM)
        self.desc_img_quality_low = 20     # 설명 PPT 저화질
        self.desc_img_quality_high = 95    # 설명 PPT 고화질

        # ──────────────────────────────────────────────────────
        # 이상치 탐지 설정 (Anomaly detection settings)
        # Z-score 기반 이상치 탐지 및 차트 표시 파라미터
        # ──────────────────────────────────────────────────────
        self.anomaly_z_threshold = 3.0            # Z-score 임계값 (threshold)
        self.max_anomaly_chart_items = 6           # 차트에 표시할 최대 이상 항목 수
        self.anomaly_chart_figsize = (10, 4.0)     # 차트 크기 (width, height in inches)
        self.anomaly_chart_dpi = 120               # 차트 해상도 (DPI)

        # ──────────────────────────────────────────────────────
        # VRAMP 조회 설정 (VRAMP lookback settings)
        # ──────────────────────────────────────────────────────
        self.vramp_lookback_days = 365  # VRAMP 데이터 조회 기간 (일)

        # ──────────────────────────────────────────────────────
        # 내부 상태 저장소 (Internal state storage)
        # ──────────────────────────────────────────────────────
        self.env = {}               # .env 환경 변수 저장소
        self.settings = {}          # YAML에서 로드된 원본 설정 (per-vehicle)
        self.generated_vars = {}    # 종속 변수 저장 (경로, HTML 등)

        # ── DC Step 매핑 (코드에서 직접 관리, YAML 의존 없음) ──
        # dc_step_to_ids: {DC layer: [step_id, ...]} 형태로 코드에서 직접 선언합니다.
        # 엔지니어가 이 dict를 직접 수정하여 DC layer ↔ step_id 매핑을 관리합니다.
        self.dc_step_to_ids = {'MFDC': ['test']}
        # dc_dict: 위 매핑으로부터 생성한 step_id → DC layer 역매핑
        self.dc_dict = {
            sid: dc
            for dc, sids in self.dc_step_to_ids.items()
            for sid in sids
        }

        # .env 파일에서 환경 변수 로드
        self._load_env_variables()

    # ================================================================
    # 환경 변수 로드 (Environment variable loading)
    # ================================================================

    def _load_env_variables(self):
        """환경 변수를 읽어서 self.env에 저장합니다.
        (Load all OS environment variables into self.env dict.)

        .env 파일의 키-값 쌍을 self.env 딕셔너리에 저장하여
        get() 메서드로 접근할 수 있게 합니다.
        """
        load_dotenv()
        for key, value in os.environ.items():
            self.env[key] = value

    # ================================================================
    # YAML 설정 로드 (YAML configuration loading)
    # ================================================================

    def load_from_yaml(self, item_name, yaml_path=None):
        """reformatter/config.yaml에서 특정 vehicle 설정을 로드합니다.
        (Load vehicle-specific settings from reformatter/config.yaml.)

        설정 파일 경로는 이 모듈(My_config.py / Main.py가 위치한 디렉토리)을
        기준으로 한 상대 경로(reformatter/config.yaml)로 해석합니다. 따라서
        현재 작업 디렉토리(cwd)와 무관하게 항상 올바른 파일을 찾습니다.

        Args:
            item_name (str): Vehicle 이름 (예: 'vehicle_A')
            yaml_path (str, optional): YAML 설정 파일 경로.
                생략 시 base_path 기준 reformatter/config.yaml 사용.

        Raises:
            ValueError: item_name이 YAML에 존재하지 않을 경우
        """
        if yaml_path is None:
            yaml_path = os.path.join(self.base_path, 'reformatter', 'config.yaml')

        with open(yaml_path, "r", encoding='utf-8') as file:
            config_data = yaml.safe_load(file)

        if item_name not in config_data:
            raise ValueError(f"{item_name} 항목이 config.yaml에 존재하지 않습니다.")

        # 원본 설정 저장 (Store raw YAML settings)
        self.settings = config_data[item_name]

        # NOTE: dc_step_to_ids / dc_dict는 __init__에서 코드로 직접 선언합니다.
        #       (YAML 의존 없음)

        # 종속 변수 생성 (Generate dependent variables)
        self._generate_dependent_vars()

    # ================================================================
    # 종속 변수 생성 (Dependent variable generation)
    # ================================================================

    def _generate_dependent_vars(self):
        """로드된 설정을 기반으로 종속 변수를 생성합니다.
        (Generate dependent variables from loaded settings.)

        다음 항목들을 생성합니다:
        - url: 메일 발송 API URL
        - html_code: 리포트 HTML 템플릿 (templates/report.html에서 로드)
        - 파일 시스템 경로: ROOT, DB, Report, Log 디렉토리 및 파일
        """

        # ── 메일 발송 API URL 생성 ──
        self.generated_vars["url"] = (
            f"http://catalog.itplatform.sec.samsung.net:7979/apim/mail/3/api/v1/"
            f"shared/mail/mails2/send/attach"
            f"?systemId={self.settings['YOUR_PROJECT']}"
            f"&loginUser.login={self.settings['KNOXID']}"
        )

        # ── HTML 템플릿 로드 (Load HTML template from file) ──
        # templates/report.html 파일에서 리포트 HTML을 로드하고
        # vehicle별 플레이스홀더를 치환합니다.
        template_path = os.path.join(self.base_path, 'templates', 'report.html')
        if os.path.exists(template_path):
            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()
            # 플레이스홀더를 vehicle별 설정값으로 치환
            template = template.replace('{{node}}', self.settings.get('node', ''))
            template = template.replace('{{vehicle}}', self.settings.get('vehicle', ''))
            template = template.replace('{{system_admin}}', self.settings.get('system_admin', ''))
            self.generated_vars['html_code'] = template
        else:
            # 템플릿 파일이 없으면 빈 문자열 (fallback)
            self.generated_vars['html_code'] = ''

        # ── DB 경로 생성 (DB directory paths) ──
        # ROOT: 실행 기반 디렉토리
        # DB: 데이터베이스 저장 디렉토리
        # DB_et_daily: vehicle별 일일 ET 데이터 디렉토리
        self.generated_vars["ROOT"] = (
            os.path.join(self.base_path, 'RUN') + os.sep)
        self.generated_vars["DB"] = (
            os.path.join(self.base_path, 'RUN', 'DB') + os.sep)
        self.generated_vars["DB_et_daily"] = (
            os.path.join(self.base_path, 'RUN', 'DB',
                         self.settings['vehicle'] + '_daily') + os.sep)

        # ── Report 경로 생성 (Report directory paths) ──
        # Report: 리포트 루트 디렉토리
        # low_qual_ppt_save_path: 메일용 저화질 PPT 저장 경로
        # html_save_path: HTML 리포트 저장 경로
        self.generated_vars["Report"] = (
            os.path.join(self.base_path, 'RUN', 'Report') + os.sep)
        self.generated_vars["low_qual_ppt_save_path"] = (
            os.path.join(self.base_path, 'RUN', 'Report',
                         self.settings["vehicle"], 'Mail') + os.sep)
        self.generated_vars["html_save_path"] = (
            os.path.join(self.generated_vars["Report"],
                         self.settings["vehicle"], 'HTML') + os.sep)

        # ── Log 경로 생성 (Log file paths) ──
        # 각종 로그 파일 경로를 vehicle별로 생성합니다.
        self.generated_vars["log"] = (
            os.path.join(self.base_path, 'RUN', 'log') + os.sep)
        self.generated_vars["query_log"] = os.path.join(
            self.base_path, 'RUN', 'log',
            self.settings['vehicle'] + '_query_log.txt')
        self.generated_vars["loop_log"] = os.path.join(
            self.base_path, 'RUN', 'log',
            self.settings['vehicle'] + '_loop_log.txt')
        self.generated_vars["error_log"] = os.path.join(
            self.base_path, 'RUN', 'log',
            self.settings['vehicle'] + '_error_log.txt')
        self.generated_vars["et_log_path"] = os.path.join(
            self.base_path, 'RUN', 'log',
            self.settings['vehicle'] + '_et_log.csv')
        self.generated_vars["Final_et_log_path"] = os.path.join(
            self.base_path, 'RUN', 'log',
            self.settings['vehicle'] + '_et_log_Final.csv')
        self.generated_vars["running_log"] = os.path.join(
            self.base_path, 'RUN', 'log', 'running_log.txt')

    # ================================================================
    # DC Step 조회 메서드 (DC Step lookup methods)
    # ================================================================

    def get_dc_step_from_id(self, step_id, default=None):
        """step_id로부터 DC step 이름을 조회합니다.
        (Look up DC step name from a step_id using dc_dict.)

        Args:
            step_id (str): 조회할 Step ID (예: 'NU467300')
            default: step_id가 없을 때 반환할 기본값

        Returns:
            str or default: DC step 이름 (예: 'M1DC') 또는 기본값
        """
        return self.dc_dict.get(step_id, default)

    def get_step_ids_from_dc_step(self, dc_step, default=None):
        """DC step 이름으로부터 해당하는 step_id 목록을 조회합니다.
        (Get list of step_ids for a given DC step from self.dc_step_to_ids.)

        Args:
            dc_step (str): DC step 이름 (예: 'M1DC')
            default: dc_step이 없을 때 반환할 기본값

        Returns:
            list or default: step_id 리스트 또는 기본값
        """
        return self.dc_step_to_ids.get(dc_step, default)

    # ================================================================
    # 범용 값 조회 (General value lookup)
    # ================================================================

    def get(self, key, default=None):
        """설정값을 우선순위에 따라 조회합니다.
        (Retrieve a config value by priority: settings → generated_vars → env → instance attr.)

        조회 우선순위 (lookup priority):
        1. self.settings   – YAML에서 로드된 vehicle별 설정
        2. self.generated_vars – 종속적으로 생성된 변수 (경로, HTML 등)
        3. self.env         – .env 환경 변수
        4. self 인스턴스 속성 – __init__에서 정의된 전역 설정

        Args:
            key (str): 조회할 설정 키
            default: 키가 없을 때 반환할 기본값

        Returns:
            설정값 또는 기본값
        """
        # 1. YAML 원본 설정에서 검색
        if key in self.settings:
            return self.settings[key]
        # 2. 종속 변수에서 검색
        if key in self.generated_vars:
            return self.generated_vars[key]
        # 3. 환경 변수에서 검색
        if key in self.env:
            return self.env[key]
        # 4. 인스턴스 속성에서 검색
        if hasattr(self, key):
            return getattr(self, key)
        # 5. 기본값 반환
        return default


# ================================================================
# 글로벌 설정 객체 (Global configuration singleton)
# ================================================================
# 프로젝트 전체에서 이 객체를 import하여 사용합니다.
#   from My_config import GLOBAL_CONFIG
GLOBAL_CONFIG = Config()
