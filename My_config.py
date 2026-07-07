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


# ============================================================
# 리포트 HTML 템플릿 (내장)
# ------------------------------------------------------------
# 과거 templates/report.html 파일로 분리되어 있었으나, 사내 이식 시
# 코어 4개 파일(Main.py / My_config.py / My_Function.py / anomaly_engine.py)
# 외 추가 파일이 따라가지 않도록 이 모듈 안에 직접 내장합니다.
# {{node}} / {{vehicle}} / {{system_admin}} 플레이스홀더는
# _generate_dependent_vars()에서 vehicle별 값으로 치환됩니다.
# Main.py는 'sub_title' 및 <div id="targetN"></div> 영역을 추가 치환합니다.
# ============================================================
_REPORT_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>[{{node}} HOL] {{vehicle}} Auto Report</title>
    <style>
        /* ============================================================
           Auto Report HTML Template – Engineer Report Style
           ============================================================
           이 CSS는 전문 엔지니어 리포트 스타일을 정의합니다.
           Color scheme: Navy (#003366) primary, dark borders (#2c2c2c)
           ============================================================ */

        /* ── 기본 스타일 (Base styles) ── */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        /* 여백은 본문 wrapper div의 inline style로 지정(메일/포워딩 동일 표시) — body엔 미지정 */
        body {
            font-family: 'Segoe UI', Arial, '맑은 고딕', Malgun Gothic, sans-serif;
            font-size: 12px;
            background-color: #ffffff;
            color: #1a1a1a;
            line-height: 1.5;
            max-width: 100%;
            margin: 0;
            padding: 0;
        }

        /* ── 제목 스타일 (Heading styles) ── */
        h1, h2, h3, h4 {
            color: #003366;
            margin-bottom: 4px;
        }

        h2 {
            font-size: 18px;
            border-bottom: 2px solid #003366;
            padding-bottom: 6px;
            margin-top: 0;
            margin-bottom: 2px;
        }

        /* ── 링크 스타일 (Link styles) ── */
        a {
            color: #0055aa;
            text-decoration: none;
        }

        a:hover {
            text-decoration: underline;
            color: #003366;
        }

        /* ── 섹션 제목 (Section titles) ── */
        .section-title {
            border-left: 4px solid #003366;
            padding-left: 8px;
            font-size: 15px;
            font-weight: bold;
            color: #003366;
            margin-top: 20px;
            margin-bottom: 6px;
        }

        /* ── 정보 블록 (Info block) ── */
        .info-block {
            margin-bottom: 12px;
            font-size: 12px;
        }

        .info-block strong {
            font-size: 14px;
            color: #003366;
        }

        .info-block span {
            font-size: 12px;
        }

        .subtitle {
            font-size: 13px;
            font-weight: bold;
            color: #555555;
            margin-bottom: 10px;
        }

        /* ── 목차 (Table of contents) ── */
        .toc {
            margin-bottom: 16px;
        }

        .toc a {
            display: inline-block;
            margin: 1px 0;
            font-size: 12px;
        }

        /* ============================================================
           테이블 스타일 (Table styles)
           ============================================================ */

        /* ── 테이블 컨테이너 – 스크롤 및 틀고정 (Scrollable container) ── */
        .table-container {
            overflow-x: auto;
            overflow-y: auto;
            max-height: 500px;
            width: max-content;
            max-width: 100%;
            margin-top: 5px;
            margin-bottom: 15px;
        }

        /* ── 기본 테이블 (Base table) ── */
        table {
            border-collapse: collapse;
            font-size: 11px;
            width: max-content;
        }

        table, th, td {
            border: 1px solid #2c2c2c;
        }

        /* ── 테이블 헤더 – 고정 헤더 (Sticky headers) ── */
        thead th {
            position: sticky;
            top: 0;
            background: #f5f6fa;
            color: #1a1a1a;
            text-align: center;
            padding: 5px 8px;
            font-weight: bold;
            white-space: nowrap;
            z-index: 10;
            border-bottom: 2px solid #003366;
        }

        /* 멀티인덱스 두번째 헤더행 겹침 방지 */
        thead tr:nth-child(2) th {
            top: 25px;
            z-index: 9;
        }

        /* ── 셀 스타일 (Cell styles) ── */
        td {
            text-align: center;
            padding: 4px 6px;
            white-space: nowrap;
        }

        /* ── 교대 행 색상 (Alternating row colors) ── */
        tbody tr:nth-child(even) {
            background: #fafbfc;
        }

        tbody tr:hover {
            background: #eef2f7;
        }

        /* ============================================================
           Score Board 테이블 (Score board table)
           좌측 고정열: LOT_ID / category / Item
           (클래스 기반 sticky — category 셀 rowspan 병합에도 안 깨짐)
           ============================================================ */
        /* category (가장 왼쪽 고정열) — 기존 64px의 1.2배 = 77px */
        .score-board th.sb-cat, .score-board td.sb-cat {
            position: sticky;
            left: 0;
            z-index: 6;
            width: 77px; min-width: 77px; max-width: 77px;
            background-color: #ebf4ff;
        }
        /* Item (두번째 고정열, category 폭 77px 만큼 오프셋) */
        /* Item명 nowrap 유지하되 폭을 줄여(330px) 풀스크린에서 wafer #25까지 보이게 함 */
        .score-board th.sb-item, .score-board td.sb-item {
            position: sticky;
            left: 77px;
            z-index: 6;
            width: 330px; min-width: 330px; max-width: 330px;
            background-color: #ebf4ff;
            white-space: nowrap;       /* 줄바꿈 방지(한 줄 표시) */
            word-break: normal;
            overflow: visible;
            text-overflow: clip;
        }
        /* LOT_ID 헤더 (category+Item 위 colspan=2, 좌측 상단 코너 고정) */
        .score-board th.sb-frozen-lot {
            position: sticky;
            left: 0;
            z-index: 12;
            width: 407px; min-width: 407px; max-width: 407px;  /* 77(cat)+330(item) */
            text-align: center;
        }
        /* 헤더의 고정열 셀은 top(thead) + left 동시 고정(코너) → z-index 상향 */
        .score-board thead th.sb-cat,
        .score-board thead th.sb-item {
            z-index: 11;
        }
        /* wafer 열: 폭 최소화 → 풀스크린에서 25개까지 한 화면에 표시 (34→32px, ~5% 축소) */
        .score-board th.sb-waf, .score-board td.sb-val {
            width: 32px; min-width: 32px; max-width: 32px;
            padding: 2px 1px;
            font-size: 10px;
            white-space: nowrap;
        }
        table.score-board th.row_heading,
        table.score-board td.row_heading {
            text-align: center !important;
        }
        /* Score Board 컨테이너: 스크롤 없이 전체 항목을 한번에 펼침.
           overflow:visible → thead(LOT_ID/wafer 헤더행)가 '페이지' 스크롤 시 상단에 sticky 고정된다. */
        .table-container.score-board-open {
            overflow: visible !important;
            max-height: none !important;
        }
        /* sticky 헤더가 아래 셀 위로 확실히 올라오도록 헤더 배경/z-index 보강 */
        .score-board thead th {
            position: sticky;
            background: #f0f0f0;
            z-index: 10;
        }

        /* ============================================================
           Inline 테이블 (Inline table)
           ============================================================ */
        table.inline-table th.row_heading {
            text-align: left !important;
            background-color: #e2efda !important;
        }

        /* ============================================================
           Lot Detail 테이블 (Lot detail table)
           ============================================================ */
        table.lot-detail {
            font-size: 10px;
            width: max-content;
        }

        table.lot-detail thead th {
            background: #e8edf3;
            font-size: 10px;
            padding: 3px 5px;
        }

        table.lot-detail td {
            padding: 2px 5px;
        }

        /* ============================================================
           구분선 (Divider)
           ============================================================ */
        hr {
            border: none;
            border-top: 1px solid #cccccc;
            margin: 16px 0;
        }

        /* ============================================================
           인쇄 미디어 쿼리 (Print media query)
           ============================================================ */
        @media print {
            body {
                max-width: 100%;
                padding: 10px;
                font-size: 10px;
            }

            .table-container {
                max-height: none;
                overflow: visible;
            }

            thead th {
                position: static;
            }

            .score-board th.sb-cat, .score-board td.sb-cat,
            .score-board th.sb-item, .score-board td.sb-item,
            .score-board th.sb-frozen-lot {
                position: static;
            }

            tbody tr:nth-child(even) {
                background: #f5f5f5 !important;
            }

            a {
                color: #000000;
                text-decoration: none;
            }
        }
    </style>
</head>
<body>
    <!-- ============================================================
         본문 wrapper — 메일 클라이언트는 head의 style 블록과 body 스타일을 무시하므로
         글꼴/크기/색/여백을 inline으로 지정해 HTML 파일·메일 본문·포워딩에서 동일하게 표시.
         (아래 모든 inline style은 같은 이유 — head의 style 블록은 브라우저 전용
          보조(sticky/스크롤)만 담당. 주의: 이 주석 안에 태그 형태 문자열을 쓰면
          메일 sanitizer가 오파싱할 수 있어 금지)
         ============================================================ -->
    <div style="font-family:'Segoe UI', Arial, '맑은 고딕', Malgun Gothic, sans-serif; font-size:12px; color:#1a1a1a; line-height:1.5; background-color:#ffffff; padding:16px 24px;">

    <!-- ============================================================
         Header – 리포트 제목 및 부제
         ============================================================ -->
    <div id="top"></div>
    <h2 style="color:#003366; font-size:18px; border-bottom:2px solid #003366; padding-bottom:6px; margin:0 0 2px 0;">[{{node}} HOL] {{vehicle}} sub_title</h2>
    <div class="subtitle" style="font-size:13px; font-weight:bold; color:#555555; margin-bottom:10px;">&nbsp; Python-based automated system providing new DC results, charts</div>

    <!-- ============================================================
         공지사항 (Announcements)
         ============================================================ -->
    <div class="info-block" style="margin-bottom:12px; font-size:12px;">
        <strong style="font-size:14px; color:#003366;">■ 공지 사항</strong><br>
        <span style="font-size:12px;">▷ Python 기반의 새로운 {{node}} {{vehicle}} HOL DC 측정 결과 자동 메일 발송 시스템 입니다.</span><br>
        <strong style="color:#0055aa; font-size:12px;">▷ 메일 자동 분류는 아래 조건으로 설정하면 됩니다:</strong><br>
        <span style="font-size:12px;">&nbsp;&nbsp;&nbsp;(1) 보낸 사람 : {{system_admin}}<br>
        &nbsp;&nbsp;&nbsp;(2) 메일 제목 : 다음 키워드를 포함할 때 - [HOL AUTO REPORT]</span>
    </div>

    <!-- ============================================================
         문의 및 요청사항 (Contact & Requests)
         ============================================================ -->
    <div class="info-block" style="margin-bottom:12px; font-size:12px;">
        <strong style="font-size:14px; color:#003366;">■ 문의 및 요청사항</strong><br>
        <span style="font-size:12px;">▷ 수신처 추가 및 시스템 문의 : {{system_admin}}</span><br>
        <span style="font-size:12px;">▷ Lot Report Archive : <a href="https://go/pa-web" style="color:#0055aa; text-decoration:none;">DX Web System Link</a></span>
    </div>

    <!-- ============================================================
         Contents – 목차 (Table of Contents)
         ============================================================ -->
    <div class="info-block toc" style="margin-bottom:16px; font-size:12px;">
        <strong style="font-size:14px; color:#003366;">■ Contents</strong><br>
        <span style="font-size:12px;">&nbsp; <a href="#target0" style="color:#0055aa; text-decoration:none;">[0] Anomaly Trend Chart</a></span><br>
        <span style="font-size:12px;">&nbsp; <a href="#target1" style="color:#0055aa; text-decoration:none;">[1] Score Board</a></span><br>
        <span style="font-size:12px;">&nbsp; <a href="#target2" style="color:#0055aa; text-decoration:none;">[2] Inline Table</a></span><br>
        <span style="font-size:12px;">&nbsp; <a href="#target3" style="color:#0055aa; text-decoration:none;">[3] 최근 DC측정자재 상세</a></span>
    </div>

    <hr style="border:none; border-top:1px solid #cccccc; margin:16px 0;">

    <!-- ============================================================
         Section Placeholders (동적 콘텐츠 삽입 영역)
         Main.py에서 각 target div 아래에 콘텐츠를 삽입합니다.
         ============================================================ -->
    <div id="target0"></div>
    <div id="target1"></div>
    <div id="target2"></div>
    <div id="target3"></div>

    </div>
</body>
</html>
"""


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

        # ──────────────────────────────────────────────────────
        # 데이터 전처리 설정 (Data preprocessing settings)
        # ──────────────────────────────────────────────────────
        # suffixes_remove: step_id/항목명 끝에서 제거할 접미사 목록
        self.suffixes_remove = ["ZZ", "M1X1", "_P1", "_P2", "tot"]
        # prefixes_remove: step_id/항목명 앞에서 제거할 접두사 목록
        self.prefixes_remove = []

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
        # PPT 설명 문서 경로
        self.description_ppt_path = os.path.join(
            self.base_path, 'HOL_Auto_Report_Description.pptx')
        # 이상 해석 지식베이스(MD) — AI 다단계 해석의 root-cause 단계가 참고
        self.anomaly_knowledge_path = os.path.join(
            self.base_path, 'ANOMALY_KNOWLEDGE.md')
        # 메일링 리스트 엑셀 파일 경로
        self.email_list_path = os.path.join(
            self.base_path, 'HOL_Auto_Report_Mailing_List.xlsx')

        # ──────────────────────────────────────────────────────
        # 시트 이름 설정 (Sheet name settings)
        # ──────────────────────────────────────────────────────
        self.inline_file_sheet = 'INLINE_1'     # Inline 데이터 시트명

        # ──────────────────────────────────────────────────────
        # Score Board 색상 (연속 보간 / continuous) — PPT·HTML 공통
        #   score_color_scale: [(점수, '#hex'), ...] 오름차순 제어점. 사이 값은 RGB 선형보간.
        #   글자색은 배경 밝기로 자동(밝으면 검정/어두우면 흰색).
        #   score_color_scale_by_item: ITEM(ALIAS)별 커스텀 스케일(없으면 기본 사용).
        #     예) 어떤 항목은 90점도 빨강, 어떤 항목은 90점이 노랑 — 항목별로 제어점만 다르게.
        #   값→색은 GLOBAL_CONFIG.score_color(value, item)으로 산출(PPT/HTML 동일 호출).
        # ──────────────────────────────────────────────────────
        self.score_color_scale = [
            (0.0,   '#C00000'),   # 0점: 진빨강
            (50.0,  '#FF0000'),   # 50:  빨강
            (70.0,  '#FFC000'),   # 70:  주황
            (90.0,  '#92D050'),   # 90:  연초록
            (100.0, '#00B050'),   # 100: 초록
        ]
        self.score_color_na = '#555555'   # 측정 없음(N/A) 회색

        self.score_color_scale_by_item = {
            # 'VTH_N': [(0,'#C00000'), (90,'#FF0000'), (95,'#FFC000'), (100,'#00B050')],
            # 'IDSAT_N': [(0,'#C00000'), (80,'#FFC000'), (100,'#00B050')],
        }

        # ──────────────────────────────────────────────────────
        # 리포트 디자인 테마 설정 (Report Design Theme Settings)
        # 엔지니어가 PPT 및 차트의 색상, 폰트, 여백 등을 쉽게 변경할 수 있습니다.
        # ──────────────────────────────────────────────────────
        self.theme_font_family = 'Segoe UI'        # 기본 폰트
        self.theme_title_color = (31, 73, 125)     # 슬라이드/차트 제목·헤더 색상 (Dark Blue, 전 슬라이드 통일)

        # ──────────────────────────────────────────────────────
        # GPT 연동 기능 ON/OFF (GPT feature toggles)
        # GPT(gpt_oss_client) 연결이 필요한 기능을 끄거나 켭니다.
        # False로 두면 해당 GPT 기능을 완전히 스킵합니다(호출 자체를 하지 않음).
        # 오프라인/사내망 외 환경이거나 GPT 미사용 시 False로 설정하세요.
        # ──────────────────────────────────────────────────────
        self.use_gpt_summary = True        # GPT 리포트 요약(요약문) 사용 여부 (텍스트 요약에만 영향)
        self.use_gpt_multistep = True      # AI 다단계 해석(triage→root-cause→final) 사용 (use_gpt_summary=True일 때)
        self.use_email_send = False        # 사내 메일 API로 PPT+HTML 발송 on/off (True면 리포트 발행 후 메일 전송)
        self.mail_attach_limit = 10        # 메일 API 첨부 최대 개수(본문 인라인 이미지 + 첨부파일 합계). 초과 시 전체 HTML을 첨부하고 본문 초과 이미지는 안내 문구로 대체(발송 항상 성공)
        self.use_s3_upload = True          # 생성 PPT의 S3(DX) 업로드 on/off
        self.use_description_page = True   # PPT CAT2 간지(Description) 페이지 삽입 on/off
        # 이상 Trend chart([0] 섹션) 표시 여부.
        #   - AI(GPT) 사용 여부와 무관하게 동작합니다.
        #   - use_gpt_summary=False 이거나 GPT 호출이 실패해도,
        #     metrics_dict 기반 코드 우선순위로 이상 Trend chart를 첨부합니다.
        self.show_anomaly_trend_chart = True
        self.anomaly_trend_chart_top_n = 5   # 이상 Trend chart 최대 개수(이상+주의 합산, 통계 자동분석 상위와 동일)
        self.anomaly_deviation_sigma = 1.5   # 코드 이상판정 임계: 평균 이탈도(sigma) 초과 시 이상

        # ──────────────────────────────────────────────────────
        # Commonality / 이상 해석 엔진 (anomaly_engine.analyze_commonality)
        # AI 없이도 코드로 동작하는 1차 자동 해석의 임계값들.
        # 모두 이 파일에서 조정 → README "Anomaly Trend Chart 우선순위" 참고
        # ──────────────────────────────────────────────────────
        self.anomaly_lot_median_sigma = 2.0      # 주의(median): target wafer median이 '제품 wafer median 분포'에서 이 σ 초과 이탈 시 주의
        self.anomaly_lot_dispersion_ratio = 2.0  # 주의(산포): target wafer 내부 산포가 '보통 wafer 산포'의 이 배수 초과 시 주의
        self.anomaly_median_low_sigma = 2.0      # 지식규칙 median_low(): target median이 제품 median 대비 이 σ 이상 낮으면 True
        # ── 통계 자동분석 제외 항목 ──
        #   여기에 넣은 ITEM(ALIAS)은 통계 자동분석(이상/주의 finding·우선순위·Anomaly Trend Chart)에서
        #   완전히 제외된다(Score Board/Trend 등 나머지 리포트에는 그대로 나옴).
        #   - 대소문자 무시, fnmatch 와일드카드(*,?) 지원. 예: 'MAWIN_*'는 MAWIN 파생컬럼 전부 제외.
        #   - 파생/마진 컬럼처럼 통계 이상으로 잡을 필요 없는 항목을 걸러 우선순위 노이즈를 줄이는 용도.
        self.anomaly_exclude_items = [
            'MAWIN_minus_margin', 'MAWIN_plus_margin', 'MAWIN_ovl_index', 'MAWIN_new',
        ]
        self.scoreboard_wfmap_min_pts = 50       # Score Board(HTML)에 wafer별 WF MAP을 넣을 최소 측정 point 수
        # WF MAP 제외 키워드: item(ALIAS)명에 아래 키워드가 포함되면 측정 point 수와
        # 무관하게 Score Board WF MAP을 표시하지 않는다. (예: PCHK 측정 항목)
        # 새 키워드를 추가하려면 이 리스트에 문자열을 넣으면 된다.
        self.wfmap_exclude_keywords = ['PCHK']

        # ── [0] Anomaly Trend Chart 우측 spec-out WF MAP ──
        #   SPEC OUT(이상) 항목은 Trend 차트 우측에 spec-out(=flier) 칩맵을 최대한 많이 그린다.
        #   (통과 칩=회색, spec 이탈 칩=빨강). target lot의 spec-out wafer는 '모두' 우선 표시하고
        #   (lot 25매 전부 spec이면 25장 다 표시), 남는 칸은 다른 lot을 TKOUT_TIME 최신순으로 채운다.
        #   anomaly_wfmap_max_count는 'target 외'를 포함한 총 표시 상한(target spec wafer는 상한과 무관하게 모두 표시).
        self.anomaly_wfmap_specout = True        # spec-out WF MAP 표시 on/off
        self.anomaly_wfmap_max_count = 42        # 총 표시 상한(target spec wafer는 항상 전부 표시)

        # ── PPT Trend chart: 특정 항목은 site(모든 값) 대신 tkout_time 기준 집계점으로 표시 ──
        #   {항목명(ALIAS): 'P10'} 형식. 'P10'=10퍼센타일, 'P90', 'MEDIAN'(=P50), 'MEAN' 지원.
        #   각 측정(tkout_time=wafer 측정)별로 site 값을 1점으로 집계해 Trend에 찍는다.
        self.trend_tkout_agg = {'MAWIN': 'P10'}

        # ── 자연어 규칙(NL_RULES) 발행 시 바로 적용 여부 ──
        #   True(기본): NL_RULES의 자연어를 문구별 캐시(RUN/AI/nl_rules_map.json)로 변환해 발행 시
        #              바로 적용. 같은 문구 → 항상 같은 코드. 미리보기/캐시정비는 `--convert-nl-rules`.
        #   False: 발행 시 자동 적용 끔(수기 [RULE] + `--convert-nl-rules-md`로 명시 적용만).
        self.anomaly_nl_autocompile = True

        # 불량 모드(Defect Mode) 판정/조합 해석은 코드가 하지 않는다.
        #   - 코드는 각 Index의 단일 이상(spec-out / median·std 이탈)만 산출.
        #   - 불량 모드 우선순위 판정표는 ANOMALY_KNOWLEDGE.md('불량 모드 판정표')에서 관리하며,
        #     AI(use_gpt_summary)가 연결된 경우에만 상단 요약에 불량 모드를 해석/표기한다.

        # ── 특이맵(spec-out 공간 패턴) 판정 — 규칙 목록 기반, 하드코딩 기본규칙 없음 ──
        #   판정은 **오직 아래 anomaly_pattern_rules(list)로만** 동작한다.
        #   ⚠️ None 이거나 빈 리스트([])면 **특이맵(공간 패턴) 판정을 아예 하지 않는다**
        #      (spec_out_pattern 라벨 미생성 — "전면성/Center 집중/Edge ring" 등 안 잡힘).
        #   활성화하려면 아래 예시처럼 원하는 규칙만 원하는 순서로 지정한다(위에서부터 '먼저 통과'한 라벨 채택).
        #   각 규칙 = {'name','type',<type별 파라미터>}. type/파라미터·판정식은 README '특이맵' 절 참조.
        #   예시(전체 기본 세트 — 필요한 것만 골라 쓰거나 임계 조정):
        #   self.anomaly_pattern_rules = [
        #       {'name': '전면성',            'type': 'global',      'min_share': 0.5},
        #       {'name': '세로 줄성',         'type': 'line',        'axis': 'x', 'max_lanes': 2, 'min_pts': 4},
        #       {'name': '가로 줄성',         'type': 'line',        'axis': 'y', 'max_lanes': 2, 'min_pts': 4},
        #       {'name': 'Center 집중',       'type': 'radius_band', 'r_min': 0.0,  'r_max': 0.45, 'cover': 0.7},
        #       {'name': 'Edge ring',         'type': 'radius_band', 'r_min': 0.85, 'r_max': 1.01, 'cover': 0.7},
        #       {'name': 'Middle 환형',       'type': 'radius_band', 'r_min': 0.45, 'r_max': 0.85, 'cover': 0.7},
        #       {'name': 'k시 방향 클러스터', 'type': 'clock',       'min_rnorm': 0.4, 'resultant': 0.92, 'min_frac': 0.75},
        #       {'name': '사분면',            'type': 'quadrant',    'cover': 0.7},
        #       {'name': '반구',              'type': 'half',        'cover': 0.75},
        #   ]
        #   각 규칙의 평가값·통과여부는 anomaly_basis_<lot>.json의
        #   spec_out_pattern_stats['rules']에 trace로 남아 "왜 이 특이맵인지" 확인 가능.
        self.anomaly_pattern_rules = None   # 기본: 특이맵 판정 OFF (규칙 지정 시에만 동작)
        #   전역 옵션(규칙 공통) — dict로 부분 override:
        #     min_pts(3)                : 패턴 판정 최소 unique 좌표 수
        #     y_positive_up(True)       : 좌표 y+가 웨이퍼 위(12시) 방향인지
        #     gate_few_wafer_max(2)     : 이상 wafer가 이 수 이하면 '소수 wafer'로 간주
        #     gate_few_wafer_min_pts(4) : 소수 wafer일 때 특이맵 판정에 필요한 최소 spec-out pt
        #                                 (1~2 wafer & <4pt → 판정 보류, basis에 gated 사유 기록)
        #     repeat_min_wafers(3)      : 이 수 이상 wafer 발생 시 wafer간 반복성 코멘트 검사 —
        #                                 같은 좌표가 이 수 이상 wafer에서 out → '동일 shot 반복'
        #     similar_overlap_frac(0.5) : 동일 shot 반복이 없어도 out 좌표의 이 비율 이상이
        #                                 2개 wafer 이상 겹치면 '유사 위치 반복' 코멘트
        self.anomaly_pattern_thresholds = {}

        # ── 측정 순서(Measurement-Order) 기반 판정 ──
        #   별도 설정 없음 — ANOMALY_KNOWLEDGE.md [RULE]의 조건 함수
        #   seq_out(n)/seq_mostly_dead(f)/seq_front_heavy 로만 판정한다(룰 관리 일원화).
        #   측정 순서 = WF MAP 좌상단 기준 chip_x 먼저 증가 → chip_y 증가.

        # ── AI 판정 예시(few-shot) — RUN/EXAMPLE/*.md (없어도 동작) ──
        #   후행적으로 불량 모드가 확정된 사례를 md 파일로 넣으면 AI Final 판정에 예시로 주입된다.
        #   파일명이 '_'로 시작하면(_TEMPLATE.md 등) 스킵. 작성법은 README 'AI 판정 예시' 참조.
        self.ai_examples_dir = os.path.join('RUN', 'EXAMPLE')
        self.ai_examples_max = 5           # 주입할 예시 파일 최대 개수(파일명 정렬순)
        self.ai_examples_max_chars = 6000  # 예시 총 길이 상한(프롬프트 크기 보호)

        # ── AI 호출 모드 ──
        #   'multi'(기본) : Triage → Root-cause → Final 3회 호출(단계별 정제, 약한 모델에 유리)
        #   'single'      : Final 1회 호출(비용/지연 1/3, 단계간 오류 전파 없음 — 강한 모델 권장)
        #   Final 응답이 JSON 형식이 아니면 어느 모드든 1회 자동 재시도 후 폴백.
        self.ai_stage_mode = 'multi'

        # ──────────────────────────────────────────────────────
        # PPT 차트 렌더링 설정 (Chart rendering settings for PPT)
        # 엔지니어가 PPT에 삽입되는 차트의 해상도/압축 품질을 직접 조정합니다.
        # 화질↑ 시 용량도 함께 증가하므로 PPT 10MB 한도(목표 5~7MB)를 고려해 조정하세요.
        # ──────────────────────────────────────────────────────
        # 용량 목표: index 80개(각 WF MAP 50pt+ 포함) 기준 PPT ≈ 7MB가 되도록 튜닝.
        #   index당 이미지 6종 = box/trend/radius/cum(chart 품질) + WF MAP(map 품질, 최대 용량) + colorbar.
        #   더 줄이려면 순서: 1) ppt_map_jpg_quality  2) ppt_chart_jpg_quality  3) ppt_chart_dpi
        self.ppt_chart_dpi = 125           # PPT 삽입 차트 해상도 (DPI). 높이면 선명+용량↑
        self.ppt_chart_jpg_quality = 55    # 라인/박스/트렌드/CDF/레전드 JPEG 품질 (1~95, 용량의 주 레버)
        self.ppt_map_jpg_quality = 38      # WF MAP(연속색 산점) JPEG 품질 (index당 최대 용량 항목)

        # ── HTML 리포트용 이미지 해상도(DPI) — PPT와 독립적으로 조정 ──
        # HTML [0] Anomaly Trend chart(RUN/TEMP png)와 Score Board/anomaly WF MAP의 DPI.
        # 높이면 HTML 이미지가 선명해지지만 HTML 용량이 커진다.
        self.html_chart_dpi = 100          # HTML에 들어가는 Trend chart PNG 해상도
        self.html_wfmap_dpi = 100          # HTML에 들어가는 WF MAP PNG 해상도(Score Board/anomaly)

        # ──────────────────────────────────────────────────────
        # 병렬 렌더링 설정 (Parallel chart rendering)
        # 차트/WF MAP 렌더링(matplotlib)을 워커 프로세스로 병렬화하여 발행 속도를 높인다.
        # 워커 수는 실행 환경의 CPU 코어 수와 '가용' 메모리를 보고 자동 결정된다:
        #   workers = min(코어수, (가용GB - reserve) / per_worker, 상한 8)
        #   예) 4코어/50GB → 4워커, 2코어/10GB → 2워커, 가용 메모리 부족 → 1(직렬 폴백)
        # ──────────────────────────────────────────────────────
        self.parallel_workers = 0             # 워커 수 강제 지정 (0=환경 보고 자동 결정)
        self.parallel_max_workers = 8         # 자동 결정 시 상한
        self.parallel_mem_per_worker_gb = 1.2  # 워커 1개당 예상 메모리(GB) — pandas/matplotlib 상주 + 작업분
        self.parallel_reserve_gb = 3.0        # 메인 프로세스(merged_df/PPT 조립) 몫으로 남겨둘 가용 메모리(GB)

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

        # ── Description 페이지 복사 시 내장 이미지 재압축(용량 절감) ──
        #   설명 슬라이드를 그대로 복사하면 원본 고해상도 이미지(수 MB~15MB)가 그대로 들어가
        #   PPT 용량이 커진다. 아래 값으로 다운스케일+JPEG 재압축해 약 2MB 수준으로 줄인다.
        #   description_image_recompress=False면 원본 그대로 복사(재압축 안 함).
        self.description_image_recompress = True   # 설명 이미지 재압축 on/off
        self.description_image_max_px = 2400       # 이미지 최대 변(px) — 이보다 크면 축소(≈2MB 목표)
        self.description_image_jpeg_quality = 85   # 재압축 JPEG 품질(0~100)

        # ──────────────────────────────────────────────────────
        # 이상치 탐지 설정 (Anomaly detection settings)
        # Z-score 기반 이상치 탐지 및 차트 표시 파라미터
        # ──────────────────────────────────────────────────────
        self.anomaly_z_threshold = 3.0            # Z-score 임계값 (threshold)
        self.max_anomaly_chart_items = 6           # 차트에 표시할 최대 이상 항목 수
        self.anomaly_chart_figsize = (10, 4.0)     # 차트 크기 (width, height in inches)
        self.anomaly_chart_dpi = 120               # 차트 해상도 (DPI)

        # ── Anomaly 상세(통계 자동 분석) PPT 페이지 분할 ──
        #   1페이지: Rule Check 결과 + 첫 N개 finding / 2페이지부터: 페이지당 M개 finding
        self.anomaly_detail_first_page_items = 5   # 1페이지 finding 수
        self.anomaly_detail_page_items = 15        # 2페이지부터 페이지당 finding 수

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
    # Score Board 색상 (연속 보간) — PPT/HTML 공통 호출
    # ================================================================
    @staticmethod
    def _hex2rgb(h):
        h = str(h).lstrip('#')
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    @staticmethod
    def _rgb2hex(r, g, b):
        return '#{:02X}{:02X}{:02X}'.format(int(round(r)), int(round(g)), int(round(b)))

    def score_color(self, value, item=None):
        """점수(0~100) → (배경 hex, 글자 hex). 연속 보간. ITEM별 스케일 override 지원.
        측정 없음/비수치는 (score_color_na, 흰색) 반환."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return (self.score_color_na, '#ffffff')
        if v != v:   # NaN
            return (self.score_color_na, '#ffffff')
        scale = None
        if item is not None:
            scale = (self.score_color_scale_by_item or {}).get(item)
        if not scale:
            scale = self.score_color_scale
        scale = sorted(scale, key=lambda p: p[0])
        if v <= scale[0][0]:
            bg = scale[0][1]
        elif v >= scale[-1][0]:
            bg = scale[-1][1]
        else:
            bg = scale[-1][1]
            for (v0, c0), (v1, c1) in zip(scale[:-1], scale[1:]):
                if v0 <= v <= v1:
                    t = (v - v0) / (v1 - v0) if v1 > v0 else 0.0
                    a, b = self._hex2rgb(c0), self._hex2rgb(c1)
                    bg = self._rgb2hex(*(a[i] + (b[i] - a[i]) * t for i in range(3)))
                    break
        r, g, b = self._hex2rgb(bg)
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        fg = '#000000' if lum > 0.6 else '#ffffff'
        return (bg, fg)

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
        - html_code: 리포트 HTML 템플릿 (모듈 내장 _REPORT_HTML_TEMPLATE에서 로드)
        - 파일 시스템 경로: ROOT, DB, Report, Log 디렉토리 및 파일
        """

        # ── 메일 발송 API URL 생성 ──
        self.generated_vars["url"] = (
            f"http://catalog.itplatform.sec.samsung.net:7979/apim/mail/3/api/v1/"
            f"shared/mail/mails2/send/attach"
            f"?systemId={self.settings['YOUR_PROJECT']}"
            f"&loginUser.login={self.settings['KNOXID']}"
        )

        # ── HTML 템플릿 로드 (내장 상수에서 로드) ──
        # 외부 templates/report.html 파일 대신 모듈 내장 _REPORT_HTML_TEMPLATE를
        # 사용하고, vehicle별 플레이스홀더를 치환합니다.
        template = _REPORT_HTML_TEMPLATE
        template = template.replace('{{node}}', self.settings.get('node', ''))
        template = template.replace('{{vehicle}}', self.settings.get('vehicle', ''))
        template = template.replace('{{system_admin}}', self.settings.get('system_admin', ''))
        self.generated_vars['html_code'] = template

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
        # 통합 로그: query/loop/error 로그를 '제품명_log.txt' 하나로 합침(rotation은 코드에서 30MB).
        _prod_name = self.settings.get('prod', self.settings['vehicle'])
        _unified_log = os.path.join(self.base_path, 'RUN', 'log', f'{_prod_name}_log.txt')
        self.generated_vars["unified_log"] = _unified_log
        self.generated_vars["query_log"] = _unified_log
        self.generated_vars["loop_log"] = _unified_log
        self.generated_vars["error_log"] = _unified_log
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
