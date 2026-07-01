# -*- coding: utf-8 -*-
"""Auto Report 유틸리티 함수 모듈.

이 모듈은 Main.py에서 사용하는 모든 유틸리티 함수를 포함합니다.
- 데이터 쿼리: etdata_query(), inlinedata_query(), wipdata_query()
- PPT 생성: make_title_page(), insert_score_board(), insert_plots()
- 데이터 변환: Reformatize(), apply_style_by_index()
- 유틸리티: log_to_file(), clear_temp_inside_run()

사용법: from My_Function import *
"""

# ===================================================================
#  표준 라이브러리 (Standard Library Imports)
# ===================================================================
import os
import re
import gc
import glob
import time
import traceback
from datetime import datetime, timedelta, date

# ===================================================================
#  서드파티 라이브러리 (Third-party Imports)
# ===================================================================
import numpy as np
import pandas as pd

# ===================================================================
#  프로젝트 내부 모듈 (Project Internal Imports)
# ===================================================================
from My_config import GLOBAL_CONFIG
from bigdataquery import getData


# ===================================================================
#  유틸리티 함수 (Utility Functions)
# ===================================================================

def log_to_file(message, log_path):
    """타임스탬프와 함께 메시지를 로그 파일에 기록.

    Parameters
    ----------
    message : str
        로그에 기록할 메시지 문자열.
    log_path : str
        로그 파일의 절대 경로. 부모 디렉토리가 없으면 자동 생성.
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"[WARN] log_to_file 실패: {e}")


def get_email_list(file_path, target_group, default_group='HOL', domain="@url"):
    """메일링 리스트 엑셀에서 수신 그룹(시트)의 KNOX_ID를 읽어 수신자 리스트를 반환.

    target_group에 해당하는 시트가 있으면 그 시트를, 없으면 default_group 시트를 사용한다.
    각 KNOX_ID에 '@'가 없으면 domain을 붙여 이메일을 구성하고,
    메일 API용 [{"email", "recipientType":"TO", "seq"}] 형태로 반환한다.
    """
    xls = pd.ExcelFile(file_path)
    sheet_name = target_group if target_group in xls.sheet_names else default_group
    df = xls.parse(sheet_name)
    email_list = df['KNOX_ID'].dropna().drop_duplicates(keep='first').tolist()
    domain = domain.strip()
    final_email_list = [
        {
        "email": email.strip() if "@" in email else f"{email.strip()}{domain}",
        "recipientType": "TO",
        "seq": i
        }
        for i, email in enumerate(email_list, start=1)
    ]
    return final_email_list


def reformatter_verify(reformatter):
    """reformatter(csv) DataFrame의 구조·내용 유효성을 검증.

    ``reformatter/{vehicle}_reformatter.csv`` 가 하위 처리(쿼리·피벗·ADDP 계산·
    spec 표시·차트)에 필요한 컬럼을 모두 갖추고 있는지, REAL/ADDP 행이 올바른지
    확인합니다.

    각 단계는 통과 여부를 ``[CHECK n] ... OK/FAIL`` 형태로 터미널에 출력합니다.

    검증 항목
    ---------
    [CHECK 1] DataFrame 비어 있지 않음.
    [CHECK 2] 필수 컬럼 존재 (CATEGORY, ITEMID, ALIAS, SCALE FACTOR, ABSOLUTE,
              ADDP FORM, REPORT ORDER, SPECLOW, SPECHIGH, REPORT DIRECTION).
    [CHECK 3] 선택 컬럼 존재 (UNIT, TARGET, REPORT LOG SCALE, CAT1, CAT2, PPT_ONLY) — 누락 시 경고만.
    [CHECK 4] ITEMID 중복 없음 (ITEMID는 유일 / ALIAS 중복은 허용).
    [CHECK 5] CATEGORY 값이 {'REAL', 'ADDP'} 범위 내.
    [CHECK 6] REAL 행은 ITEMID 보유.
    [CHECK 7] ADDP 행은 ADDP FORM(수식) 보유.

    Parameters
    ----------
    reformatter : pd.DataFrame
        검증 대상 reformatter DataFrame.

    Returns
    -------
    bool
        모든 필수 검증을 통과하면 True, 아니면 False.
    """
    print("=" * 60)
    print("[reformatter_verify] reformatter 검증 시작")
    print("=" * 60)

    # ── [CHECK 1] DataFrame 비어 있지 않음 ──
    print("[CHECK 1] DataFrame 비어있지 않음 검사...", end=" ")
    if reformatter is None or reformatter.empty:
        print("FAIL")
        print("  -> [ERROR] reformatter가 비어 있습니다.")
        return False
    print(f"OK (행 {len(reformatter)}개)")

    # ── [CHECK 2] 필수 열 존재 검사 ──
    # 하위 코드(Main.py/insert_plots)가 직접 참조하는 필수 컬럼
    required = [
        "CATEGORY", "ITEMID", "ALIAS", "SCALE FACTOR", "ABSOLUTE",
        "ADDP FORM", "REPORT ORDER", "SPECLOW", "SPECHIGH", "REPORT DIRECTION",
    ]
    print("[CHECK 2] 필수 열 존재 검사...", end=" ")
    missing = [c for c in required if c not in reformatter.columns]
    if missing:
        print("FAIL")
        print(f"  -> [ERROR] reformatter에 필수 컬럼이 없습니다: {missing}")
        return False
    print(f"OK ({len(required)}개 모두 존재)")

    # ── [CHECK 3] 선택 열 존재 검사 (없어도 동작하나 차트 표현에 사용) ──
    optional = ["UNIT", "TARGET", "REPORT LOG SCALE", "CAT1", "CAT2", "PPT_ONLY"]
    print("[CHECK 3] 선택 열 존재 검사...", end=" ")
    opt_missing = [c for c in optional if c not in reformatter.columns]
    if opt_missing:
        print(f"WARN (누락: {opt_missing} → 차트 표현 일부 제한)")
    else:
        print(f"OK ({len(optional)}개 모두 존재)")

    # ── [CHECK 4] ITEMID 중복 검사 (ITEMID는 유일 / ALIAS 중복은 허용) ──
    print("[CHECK 4] ITEMID 중복 검사 (ALIAS 중복은 허용)...", end=" ")
    itemid_nonnull = reformatter["ITEMID"].dropna().astype(str)
    itemid_nonnull = itemid_nonnull[itemid_nonnull.str.strip() != ""]
    dup_itemid = itemid_nonnull[itemid_nonnull.duplicated()].unique().tolist()
    if dup_itemid:
        print("FAIL")
        print(f"  -> [ERROR] reformatter ITEMID가 중복되었습니다(유일해야 함): {dup_itemid}")
        return False
    print(f"OK (ITEMID {itemid_nonnull.nunique()}개 모두 유일)")

    # ── [CHECK 5] CATEGORY 값 검사 ({'REAL', 'ADDP'} 범위) ──
    print("[CHECK 5] CATEGORY 값 검사...", end=" ")
    cats = set(reformatter["CATEGORY"].dropna().astype(str).str.upper().unique())
    unknown = cats - {"REAL", "ADDP"}
    if unknown:
        print("FAIL")
        print(f"  -> [ERROR] reformatter CATEGORY에 알 수 없는 값이 있습니다: {unknown}")
        return False
    print(f"OK (CATEGORY={sorted(cats)})")

    cat_upper = reformatter["CATEGORY"].astype(str).str.upper()

    # ── [CHECK 6] REAL 행 ITEMID 보유 검사 ──
    print("[CHECK 6] REAL 행 ITEMID 보유 검사...", end=" ")
    real_rows = reformatter[cat_upper == "REAL"]
    if real_rows["ITEMID"].isna().any():
        bad = real_rows[real_rows["ITEMID"].isna()]["ALIAS"].tolist()
        print("FAIL")
        print(f"  -> [ERROR] REAL 항목에 ITEMID가 비어 있습니다: {bad}")
        return False
    print(f"OK (REAL {len(real_rows)}행)")

    # ── [CHECK 7] ADDP 행 ADDP FORM(수식) 보유 검사 ──
    print("[CHECK 7] ADDP 행 ADDP FORM(수식) 보유 검사...", end=" ")
    addp_rows = reformatter[cat_upper == "ADDP"]
    if addp_rows["ADDP FORM"].isna().any():
        bad = addp_rows[addp_rows["ADDP FORM"].isna()]["ALIAS"].tolist()
        print("FAIL")
        print(f"  -> [ERROR] ADDP 항목에 ADDP FORM(수식)이 비어 있습니다: {bad}")
        return False
    print(f"OK (ADDP {len(addp_rows)}행)")

    print("-" * 60)
    print("[reformatter_verify] [PASS] 모든 검사 통과")
    print("=" * 60)
    return True


def extract_and_sort_numbers(x):
    """리스트 문자열에서 숫자를 추출하여 정렬된 문자열로 반환.

    문자열 형태의 리스트(예: "[3, 1, 2]")를 파싱하여
    오름차순 정렬된 쉼표 구분 문자열(예: "1, 2, 3")로 변환합니다.

    Parameters
    ----------
    x : str or list or tuple
        변환 대상. 문자열이면 eval로 파싱 시도.

    Returns
    -------
    str
        정렬된 숫자의 쉼표 구분 문자열.
    """
    if isinstance(x, str):
        try:
            x = eval(x)
        except Exception:
            return str(x)
    if isinstance(x, (list, tuple)):
        return ", ".join(str(i) for i in sorted(x))
    return str(x)


def remove_brackets(x):
    """문자열에서 대괄호·따옴표를 제거.

    wafer_id 리스트 등의 표시용 정리에 사용됩니다.
    예: "['A', 'B']" → "A, B"

    Parameters
    ----------
    x : str
        정리 대상 문자열.

    Returns
    -------
    str
        괄호와 따옴표가 제거된 문자열.
    """
    if isinstance(x, str):
        return x.replace("[", "").replace("]", "").replace("'", "")
    return str(x)


def replace_negatives_with_0(x):
    """음수값을 0으로 치환.

    Pass Rate 등 음수가 의미 없는 지표에서 사용됩니다.

    Parameters
    ----------
    x : numeric or any
        검사 대상 값.

    Returns
    -------
    numeric or any
        음수이면 0, 그 외에는 원래 값 그대로 반환.
    """
    try:
        if pd.notna(x) and float(x) < 0:
            return 0
    except (ValueError, TypeError):
        pass
    return x


def convert_target_data(x, suffixes_remove, replace_map):
    """항목명에서 지정 접미사를 제거하고 문자열을 치환.

    reformatter의 ITEMID나 ALIAS 정리에 활용됩니다.

    Parameters
    ----------
    x : str
        변환 대상 문자열.
    suffixes_remove : list[str] or None
        제거할 접미사 목록. 예: ["_AVG", "_MAX"]
    replace_map : dict or None
        치환 맵. 예: {"OLD": "NEW"}

    Returns
    -------
    str
        변환된 문자열.
    """
    if not isinstance(x, str):
        return x
    for suffix in (suffixes_remove or []):
        if x.endswith(suffix):
            x = x[: -len(suffix)]
    for old, new in (replace_map or {}).items():
        x = x.replace(old, new)
    return x


def apply_style_by_index(row):
    """Pass Rate 값에 따라 셀 배경 색상 스타일을 반환 (pandas Styler용).

    색상 기준 (Color Thresholds):
        - ≥ 99.0% : 초록색  (#00B050) — 양호 (Good)
        - ≥ 95.0% : 연초록  (#92D050) — 보통 (Normal)
        - ≥ 90.0% : 노란색  (#FFFF00) — 주의 (Caution)
        - < 90.0% : 빨간색  (#FF0000) — 경고 (Alert)
        - NaN/빈값 : 진회색 (#555555) — 측정 없음 (No Data)

    Parameters
    ----------
    row : pd.Series
        한 행의 데이터 (각 셀 = Pass Rate 값).

    Returns
    -------
    list[str]
        각 셀에 대응하는 CSS 스타일 문자열 리스트.
    """
    styles = []
    for val in row:
        if pd.isna(val) or val == "":
            styles.append("background-color:#555555;color:white")
        else:
            try:
                v = float(val)
                if v >= 99.0:
                    styles.append("background-color:#00B050;color:white")
                elif v >= 95.0:
                    styles.append("background-color:#92D050")
                elif v >= 90.0:
                    styles.append("background-color:#FFFF00")
                else:
                    styles.append("background-color:#FF0000;color:white")
            except (ValueError, TypeError):
                styles.append("background-color:#555555;color:white")
    return styles


def Reformatize(data, ALIAS, FORMULA):
    """ADDP(Arithmetic Derived Data Parameter) 수식 컬럼을 계산하여 DataFrame에 추가.

    reformatter의 ADDP 카테고리 수식(ADDP FORM)을 파싱하여 새 컬럼을 생성합니다.
    수식 내 ``{ALIAS}`` 참조는 ``pivot.get("ALIAS")`` 로 치환되어 해당 컬럼값으로
    계산됩니다.

    ADDP 수식 예시:
        "1.0*(({VTH_N} + {VTH_P}) / 2)"  →  (VTH_N + VTH_P) / 2

    ADDP → ADDP 재귀 참조
    ---------------------
    ADDP 항목의 ``{}`` 안 ALIAS가 또 다른 ADDP일 수 있습니다(ADDP가 ADDP를 참조).
    이 함수는 고정점(fixpoint) 반복 루프로 매 패스마다 "지금 계산 가능한" 항목만
    먼저 풀어 그 결과를 ``data`` 컬럼으로 추가합니다. 따라서 어떤 ADDP가 아직
    계산되지 않은 다른 ADDP를 참조하면 그 패스에서는 실패(에러)로 남겨두었다가,
    참조 대상이 채워진 다음 패스에서 해소됩니다. 결과적으로 ``{ADDP}`` → ``{ADDP}``
    → ... → 최종 real item 까지 체인을 **재귀적으로 따라가** 계산됩니다.
    (체인 깊이만큼 반복하도록 최대 ``max(10, ADDP수 + 1)`` 패스 수행)

    Parameters
    ----------
    data : pd.DataFrame
        피벗 완료된 ET 데이터 (REAL item이 컬럼으로 존재).
    ALIAS : list[str]
        ADDP 항목의 별칭(새 컬럼명) 리스트.
    FORMULA : list[str]
        ADDP 수식 문자열 리스트. ``{COL}`` 형태로 다른 컬럼(REAL/ADDP)을 참조.

    Returns
    -------
    pd.DataFrame
        ADDP 컬럼이 추가된 DataFrame.
    """
    def addpf(formula, data):
        pivot = data
        def LOG(u, df):
            return np.log10(ABS(df))
        def POWER(a, b):
            return np.power(a, b)
        def sqrt(a):
            return np.sqrt(a)
        def ABS(a):
            return np.abs(a)
        def rmax(*args):
            df_max = pd.DataFrame()
            for arg in args:
                df_max = pd.concat([df_max, arg], axis=1)
            return df_max.max(axis=1)
        def rmin(*args):
            df_min = pd.DataFrame()
            for arg in args:
                df_min = pd.concat([df_min, arg], axis=1)
            return df_min.min(axis=1)
        def MA_Window(*args):
            # set data
            x_data, y_data = [], pd.DataFrame()
            spec, compliance = [], 10
            for arg in args:
                if isinstance(arg, list):
                    x_data = np.array(arg)
                elif isinstance(arg, str) or isinstance(arg, int) or isinstance(arg, float):
                    if isinstance(spec, list):
                        spec = np.log10(float(arg))
                    else:
                        compliance = abs(float(arg))
                else:
                    y_data = pd.concat([y_data, arg.apply(lambda a: np.log10(float(a) + 1E-14))], axis=1)
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
                return df_coeffs[['minus_margin','plus_margin','ovl_index','','new']]
            else:
                dummy = [np.nan for _ in range(5)]
                return pd.DataFrame([dummy for _ in range(y_data.shape[0])], index=y_data.index, columns=['minus_margin','plus_margin','ovl_index','','new'])
        def stddev(a):
            return a.groupby([pivot["root_lot_id"], pivot["wafer_id"], pivot["tkout_time"]]).transform(np.std)
        def std(a):
            return a.groupby([pivot["root_lot_id"], pivot["wafer_id"], pivot["tkout_time"]]).transform(np.std)
        def STD(a):
            return a.groupby([pivot["root_lot_id"], pivot["wafer_id"], pivot["tkout_time"]]).transform(np.std)
        def AVG(a):
            return a.groupby([pivot["root_lot_id"], pivot["wafer_id"], pivot["tkout_time"]]).transform(np.mean)
        try:
            a = eval(formula)
        except Exception:
            a = 'error'
        return a

    # pivot 된 data에 Reformatter(ADDP) 적용 — 고정점 반복으로 ADDP→ADDP 재귀 해소
    ALIAS_LEFT = []
    FORMULA_LEFT = []
    max_passes = max(10, len(ALIAS) + 1)   # ADDP 참조 체인 깊이만큼 반복 보장
    for _ in range(max_passes):
        calnum = len(ALIAS)   # 패스 시작 시점의 미계산 항목 수
        for alias, formula in zip(ALIAS, FORMULA):
            formula_tmp = formula
            formula = formula.replace('{', '(pivot.get("')
            formula = formula.replace('}', '"))')
            a = addpf(formula, data)   # ADDP 계산 실행
            if str(type(a)) == "<class 'str'>":
                # 아직 못 푼 항목(참조 ADDP가 아직 미계산 등) → 다음 패스에서 재시도
                ALIAS_LEFT.append(alias)
                FORMULA_LEFT.append(formula_tmp)
                continue
            else:
                if isinstance(a, pd.DataFrame) and a.shape[1] > 1:
                    data[[alias + '_' + col if col != '' else alias for col in a]] = a
                else:
                    data[alias] = a
        ALIAS = ALIAS_LEFT
        FORMULA = FORMULA_LEFT
        ALIAS_LEFT = []
        FORMULA_LEFT = []

        if calnum == len(ALIAS):   # 한 패스 동안 진전이 없으면(더 이상 해소 불가) 종료
            break

    if ALIAS:
        print(f"[WARN] Reformatize: 참조를 해소하지 못한 ADDP 항목: {ALIAS}")
    return data


def clear_temp_inside_run():
    """temp_*.png 임시 파일 삭제."""
    for f in glob.glob("temp_*.png"):
        try:
            os.remove(f)
        except OSError:
            pass


def clear_anomaly_inside_run():
    """anomaly_*.png 임시 파일 삭제."""
    for f in glob.glob("anomaly_*.png"):
        try:
            os.remove(f)
        except OSError:
            pass


# ===================================================================
#  PPT 생성 함수 (PowerPoint Generation)
# ===================================================================

def make_title_page(vehicle, lot_id, step_merged):
    """코드로 직접 그리는 표지(첫 페이지) Presentation을 생성하여 반환.

    템플릿 파일을 읽지 않고 빈 16:9 슬라이드에 표지를 직접 구성합니다.
      - 중앙 대형 제목: "{vehicle} HOL Auto Report"
      - 그 아래: "{lot_id} / {step_merged}"  (step_merged = step_desc(step_id))
      - 우상단: 날짜(오늘)
      - 상/하단 네이비 액센트 바 + 중앙 구분선으로 깔끔한 디자인

    Args:
        vehicle, lot_id, step_merged: 표지에 표기할 정보.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from datetime import datetime as _dt

    FONT = GLOBAL_CONFIG.theme_font_family
    NAVY = RGBColor(*GLOBAL_CONFIG.theme_title_color)
    GREY = RGBColor(0x60, 0x6A, 0x7A)
    RECT = MSO_SHAPE.RECTANGLE

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    SW, SH = prs.slide_width, prs.slide_height

    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 빈 레이아웃
    # 배경 흰색
    bg = slide.background
    bg.fill.solid(); bg.fill.fore_color.rgb = RGBColor(255, 255, 255)

    def _bar(top, height):
        b = slide.shapes.add_shape(RECT, Inches(0), top, SW, height)
        b.fill.solid(); b.fill.fore_color.rgb = NAVY; b.line.fill.background()
        b.shadow.inherit = False
        return b

    # 상단/하단 네이비 액센트 바
    _bar(Inches(0), Inches(0.45))
    _bar(SH - Inches(0.45), Inches(0.45))

    # 우상단 발행 날짜
    today = _dt.now().strftime('%Y-%m-%d')
    date_box = slide.shapes.add_textbox(SW - Inches(3.6), Inches(0.7), Inches(3.3), Inches(0.4))
    dp = date_box.text_frame.paragraphs[0]
    dp.text = f"{today}"
    dp.alignment = PP_ALIGN.RIGHT
    dp.font.size = Pt(13); dp.font.color.rgb = GREY; dp.font.name = FONT

    # 중앙 대형 제목
    title_box = slide.shapes.add_textbox(Inches(0.6), Inches(2.55), SW - Inches(1.2), Inches(1.5))
    ttf = title_box.text_frame; ttf.word_wrap = True
    ttf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tp = ttf.paragraphs[0]
    tp.text = f"{vehicle} HOL Auto Report"
    tp.alignment = PP_ALIGN.CENTER
    tp.font.size = Pt(46); tp.font.bold = True; tp.font.color.rgb = NAVY; tp.font.name = FONT

    # 중앙 구분선 (제목 아래)
    line = slide.shapes.add_shape(RECT, SW / 2 - Inches(2.2), Inches(4.18), Inches(4.4), Pt(2.5))
    line.fill.solid(); line.fill.fore_color.rgb = NAVY; line.line.fill.background()
    line.shadow.inherit = False

    # 서브타이틀: lot_id / step_desc(step_id)
    sub_box = slide.shapes.add_textbox(Inches(0.6), Inches(4.35), SW - Inches(1.2), Inches(0.8))
    sp = sub_box.text_frame.paragraphs[0]
    sp.text = f"{lot_id}  /  {step_merged}"
    sp.alignment = PP_ALIGN.CENTER
    sp.font.size = Pt(24); sp.font.bold = False; sp.font.color.rgb = GREY; sp.font.name = FONT

    return prs


def insert_score_board(VIP_group, prs, lot_id, title, spec_data=None, config=None):
    """Pass Rate Score Board (PPT). 컬럼이 (lot, wafer) MultiIndex이면 lot별로 분리 표기.
       (단일 레벨 wafer 컬럼도 하위호환 동작 — 모두 lot_id 한 그룹으로 처리)."""
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.oxml.xmlchemy import OxmlElement

    NAVY = RGBColor(*GLOBAL_CONFIG.theme_title_color)
    FONT = GLOBAL_CONFIG.theme_font_family
    LOTBG = RGBColor(0xD9, 0xE1, 0xF2)
    WAFBG = RGBColor(0xF0, 0xF0, 0xF0)
    WHITE = RGBColor(255, 255, 255)
    BLACK = RGBColor(0, 0, 0)

    def _set_borders(cell):
        tcPr = cell._tc.get_or_add_tcPr()
        for t in ['lnL', 'lnR', 'lnT', 'lnB']:
            for el in tcPr.findall('{http://schemas.openxmlformats.org/drawingml/2006/main}' + t):
                tcPr.remove(el)
        _bi = 0
        for t in ['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB']:
            ln = OxmlElement(t); ln.set('w', '12700'); ln.set('cmpd', 'sng')
            sf = OxmlElement('a:solidFill'); sc = OxmlElement('a:srgbClr'); sc.set('val', '333333')
            sf.append(sc); ln.append(sf); tcPr.insert(_bi, ln); _bi += 1

    def _style(cell, text, bg, fg, bold=False, sz=7):
        cell.text = str(text)
        cell.fill.solid(); cell.fill.fore_color.rgb = bg
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.margin_left = Inches(0.01); cell.margin_right = Inches(0.01)
        cell.margin_top = Inches(0.0); cell.margin_bottom = Inches(0.0)
        cell.text_frame.word_wrap = False
        _set_borders(cell)
        for par in cell.text_frame.paragraphs:
            par.font.size = Pt(sz); par.font.bold = bold; par.font.name = FONT
            par.font.color.rgb = fg; par.alignment = PP_ALIGN.CENTER

    # ---- 컬럼 정규화: [(orig_col, lot, wafer), ...] / lot 정렬(target 먼저), wafer 오름차순 ----
    def _as_lw(c):
        if isinstance(c, tuple) and len(c) == 2:
            l, w = c
        else:
            l, w = lot_id, c
        try:
            w = int(float(str(w).replace('#', '')))
        except Exception:
            pass
        return (str(l), w)
    _cols = list(VIP_group.columns)
    _lw = [_as_lw(c) for c in _cols]
    _lots = sorted({x[0] for x in _lw}, key=lambda l: (0 if str(l) == str(lot_id) else 1, str(l)))
    # 측정된 (lot, wafer) → 원본 컬럼 매핑
    _present = {}
    for c, lw in zip(_cols, _lw):
        _present[(lw[0], lw[1])] = c
    # 각 lot마다 wafer #1~25를 항상 표시 (미측정 wafer는 빈 칸) → 측정 wafer 수와
    # 무관하게 테이블 열 구성/크기 고정, 모든 wafer 열 너비 동일.
    order = []   # (orig_col_or_None, lot, wafer)
    for _lot in _lots:
        for w in range(1, 26):
            order.append((_present.get((_lot, w)), _lot, w))

    chunk_size = 30
    total_pages = (len(VIP_group) - 1) // chunk_size + 1

    for page in range(total_pages):
        chunk_df = VIP_group.iloc[page * chunk_size: (page + 1) * chunk_size]
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)

        header_shape = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.58))
        header_shape.fill.solid(); header_shape.fill.fore_color.rgb = NAVY
        header_shape.line.fill.background()
        txBox = slide.shapes.add_textbox(Inches(0.2), Inches(0.06), Inches(12.7), Inches(0.5))
        p = txBox.text_frame.paragraphs[0]
        page_suffix = f" ({page+1}/{total_pages})" if total_pages > 1 else ""
        p.text = f"Score Board - {title}{page_suffix}"
        p.font.size = Pt(20); p.font.bold = True
        p.font.color.rgb = RGBColor(255, 255, 255); p.font.name = FONT

        ncols = 1 + len(order)
        nrows = len(chunk_df) + 2   # 헤더 2행(lot / wafer)
        table_height = Inches(min(6.4, 0.5 + len(chunk_df) * 0.18))
        tbl = slide.shapes.add_table(nrows, ncols, Inches(0.22), Inches(0.72),
                                     Inches(12.89), table_height).table

        item_w = 1.95   # ITEM명이 잘리지 않도록 넉넉히
        ww = max(0.14, (12.89 - item_w) / max(len(order), 1))
        tbl.columns[0].width = Inches(item_w)
        for j in range(1, ncols):
            tbl.columns[j].width = Inches(ww)   # 모든 wafer 열 동일 너비

        # 헤더: ITEM 세로 병합 + lot 가로 병합 + #wafer
        tbl.cell(0, 0).merge(tbl.cell(1, 0)); _style(tbl.cell(0, 0), "ITEM", NAVY, WHITE, True, 9)
        tbl.cell(0, 0).text_frame.word_wrap = True   # ITEM 헤더 잘림 방지
        k = 0
        while k < len(order):
            _lot = order[k][1]; k2 = k
            while k2 + 1 < len(order) and order[k2 + 1][1] == _lot:
                k2 += 1
            c0, c1 = 1 + k, 1 + k2
            if c1 > c0:
                tbl.cell(0, c0).merge(tbl.cell(0, c1))
            _style(tbl.cell(0, c0), str(_lot), LOTBG, BLACK, True, 8)
            k = k2 + 1
        for jj, (_oc, _lot, _waf) in enumerate(order):
            _style(tbl.cell(1, 1 + jj), f"#{_waf}", NAVY, WHITE, True, 7)

        # 본문
        for i, (idx, row) in enumerate(chunk_df.iterrows()):
            r = i + 2
            base = RGBColor(245, 247, 250) if i % 2 == 1 else WHITE
            _style(tbl.cell(r, 0), str(idx), base, BLACK, False, 8)
            tbl.cell(r, 0).text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
            tbl.cell(r, 0).text_frame.word_wrap = True   # ITEM명 잘림 방지(길면 줄바꿈)
            for jj, (_oc, _lot, _waf) in enumerate(order):
                cell = tbl.cell(r, 1 + jj)
                val = row[_oc] if (_oc is not None and _oc in row.index) else None
                # 연속 색상(HTML과 동일), ITEM별 스케일 override 지원
                if pd.notna(val) and str(val).strip() != "":
                    try:
                        v = float(val); txt = f"{v:.1f}"
                        _bg, _fg = GLOBAL_CONFIG.score_color(v, str(idx))
                        bg = RGBColor(*GLOBAL_CONFIG._hex2rgb(_bg))
                        fg = RGBColor(*GLOBAL_CONFIG._hex2rgb(_fg))
                    except (ValueError, TypeError):
                        txt = str(val); bg = RGBColor(*GLOBAL_CONFIG._hex2rgb(GLOBAL_CONFIG.score_color_na)); fg = WHITE
                else:
                    txt = ""; bg = RGBColor(*GLOBAL_CONFIG._hex2rgb(GLOBAL_CONFIG.score_color_na)); fg = WHITE
                _style(cell, txt, bg, fg, False, 7)

    return prs


def insert_findings_page(prs, findings, after_index=2, title="■ Anomaly 상세 (통계 자동 분석)"):
    """코드 통계 분석 Finding 전체를 1개 슬라이드로 만들어 Score Board 뒤에 삽입.

    HTML [0]에는 우선순위 상위 N건만 보이고, 전체 상세는 이 PPT 페이지를 참조.
    after_index 위치(보통 1=title + Score Board 페이지수)로 슬라이드를 이동시킨다.
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    import re as _re

    NAVY = RGBColor(*GLOBAL_CONFIG.theme_title_color)
    FONT = GLOBAL_CONFIG.theme_font_family
    # 신호등 3색(anomaly_engine._SEV_COLOR 미러): 빨강 이상 / 주황 주의 / 노랑 측정이상추정
    sev_dot = {"CRITICAL": RGBColor(0xD6, 0x27, 0x28),
               "WARNING":  RGBColor(0xF5, 0x9E, 0x0B),
               "NOTICE":   RGBColor(0xEA, 0xB3, 0x08),
               "INFO":     RGBColor(0x5D, 0x6D, 0x7E)}
    sev_label = {"CRITICAL": "이상", "WARNING": "주의", "NOTICE": "주의", "INFO": "참고"}

    def _strip(t):
        return _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', str(t))).strip()

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bg = slide.background; bg.fill.solid(); bg.fill.fore_color.rgb = RGBColor(255, 255, 255)

    hdr = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.62))
    hdr.fill.solid(); hdr.fill.fore_color.rgb = NAVY; hdr.line.fill.background()
    htf = hdr.text_frame; htf.margin_left = Inches(0.2)
    hp = htf.paragraphs[0]
    n_crit = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    n_warn = sum(1 for f in findings if f.get("severity") == "WARNING")
    n_note = sum(1 for f in findings if f.get("severity") == "NOTICE")
    # 헤더: 제목 + 신호등(●) 색 점 + 건수, 항목 사이 | 구분 (HTML head와 동일 구성)
    hp.text = ""
    _WHITE = RGBColor(255, 255, 255); _DIV = RGBColor(0xC9, 0xD2, 0xE0)

    def _hrun(text, color, sz=18, bold=True):
        rr = hp.add_run(); rr.text = text
        rr.font.size = Pt(sz); rr.font.bold = bold; rr.font.name = FONT; rr.font.color.rgb = color

    _hrun(f"{title}   —   ", _WHITE)
    _hcnt = [("CRITICAL", "이상", n_crit), ("WARNING", "주의", n_warn), ("NOTICE", "측정이상 추정", n_note)]
    for _k, (_sev, _lbl, _cnt) in enumerate(_hcnt):
        _hrun("● ", sev_dot.get(_sev, _WHITE))
        _hrun(f"{_lbl} {_cnt}", _WHITE)
        if _k < len(_hcnt) - 1:
            _hrun("  |  ", _DIV)
    hp.font.size = Pt(18); hp.font.bold = True
    hp.font.color.rgb = RGBColor(255, 255, 255); hp.font.name = FONT

    box = slide.shapes.add_textbox(Inches(0.3), Inches(0.78),
                                   prs.slide_width - Inches(0.6), prs.slide_height - Inches(1.0))
    tf = box.text_frame; tf.word_wrap = True
    if not findings:
        p = tf.paragraphs[0]; p.text = "유의미한 통계 이상 없음"
        p.font.size = Pt(12); p.font.name = FONT
    else:
        for i, f in enumerate(findings):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            sev = f.get("severity", "INFO")
            r0 = p.add_run()                      # 신호등 원(●) — severity 색
            r0.text = "● "
            r0.font.bold = True; r0.font.size = Pt(11)
            r0.font.color.rgb = sev_dot.get(sev, RGBColor(0x5D, 0x6D, 0x7E)); r0.font.name = FONT
            r1 = p.add_run()                      # 라벨+제목 — 가독성 위해 진회색 고정
            r1.text = f"{sev_label.get(sev, sev)} {f.get('title', '')}"
            r1.font.bold = True; r1.font.size = Pt(11)
            r1.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A); r1.font.name = FONT
            det = _strip(f.get("detail", ""))
            if det:
                r2 = p.add_run()
                r2.text = "  —  " + det
                r2.font.size = Pt(9); r2.font.color.rgb = RGBColor(0x55, 0x55, 0x55); r2.font.name = FONT

    # 새 슬라이드(맨 끝)를 after_index 위치로 이동 (Score Board 바로 뒤)
    try:
        sldIdLst = prs.slides._sldIdLst
        sl = list(sldIdLst)
        last = sl[-1]
        sldIdLst.remove(last)
        sldIdLst.insert(min(after_index, len(sl) - 1), last)
    except Exception as e:
        print(f"[WARN] Anomaly 상세 페이지 위치 이동 실패: {e}")
    return prs


def render_wafer_wfmaps_b64(df, item, min_pts=50, lot_prefix=None,
                            direction='BOTH', size_in=0.85, dpi=None,
                            spec_low=None, spec_high=None, by_lot=False):
    """index의 'wafer별 첫 측정(가장 이른 tkout) WF MAP'을 {wafer_id_str: base64}로 반환.

    by_lot=True이면 같은 root_lot_id의 형제 lot을 구분하기 위해 (lot, wafer)별로
    그려 키를 'f"{FAB_LOT_ID}|{int(wafer)}"' 형태로 반환한다(Score Board lot 분리 표시용).

    각 wafer에서 측정 point 수가 min_pts 이상인 경우만 포함(sparse 측정은 제외).
    Score Board에서 wafer 열 아래 행으로 정렬해 넣기 위한 용도.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import base64, io

    if dpi is None:   # HTML용 WF MAP 해상도는 My_config.html_wfmap_dpi로 관리
        dpi = getattr(GLOBAL_CONFIG, 'html_wfmap_dpi', 110)

    if item not in df.columns:
        return {}

    def _pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None
    cx = _pick('CHIP_X_ADJ', 'CHIP_X_POS', 'chip_x_pos')
    cy = _pick('CHIP_Y_ADJ', 'CHIP_Y_POS', 'chip_y_pos')
    cw = _pick('WAFER_ID', 'wafer_id')
    ct = _pick('TKOUT_TIME', 'tkout_time')
    cl = _pick('FAB_LOT_ID', 'fab_lot_id')
    if not (cx and cy and cw and ct):
        return {}

    d = df[[c for c in [cx, cy, cw, ct, cl, item] if c]].dropna(subset=[item]).copy()
    if lot_prefix and cl:
        d = d[d[cl].astype(str).str.startswith(str(lot_prefix))]
    if len(d) == 0:
        return {}
    d[ct] = pd.to_datetime(d[ct], errors='coerce')

    norm = _wfmap_norm(direction, spec_low, spec_high, d[item])
    gdim = max(int(d[cx].nunique()), int(d[cy].nunique()), 1)
    chip_pt = size_in / gdim * 72.0
    s = max(1.0, (chip_pt * 0.9) ** 2)
    cmap = _wfmap_cmap(direction)
    # 전체 wafer 격자가 잘리지 않도록 공통 축범위 + 반칩 여백 (가장자리 칩 보존)
    _gx0, _gx1 = float(d[cx].min()), float(d[cx].max())
    _gy0, _gy1 = float(d[cy].min()), float(d[cy].max())
    _xr = (_gx1 - _gx0) or 1.0; _yr = (_gy1 - _gy0) or 1.0
    _xpad = _xr / max(int(d[cx].nunique()) - 1, 1) * 0.7 if d[cx].nunique() > 1 else _xr * 0.1
    _ypad = _yr / max(int(d[cy].nunique()) - 1, 1) * 0.7 if d[cy].nunique() > 1 else _yr * 0.1

    out = {}
    # by_lot: (lot, wafer)별로 그려 형제 lot을 분리. 아니면 wafer별(lot은 첫 측정 선택).
    group_keys = [cl, cw] if (by_lot and cl) else [cw]
    for gkey, wdf in d.groupby(group_keys):
        if by_lot and cl:
            lot_val, wid = gkey
        else:
            wid = gkey if not isinstance(gkey, tuple) else gkey[0]
            lot_val = None
        # 같은 (lot,)wafer가 여러 tkout이면 가장 이른 측정 그룹 선택
        gk = [k for k in [cl, ct] if k]
        if gk:
            sizes = wdf.groupby(gk).size().reset_index(name='n')
            sizes = sizes[sizes['n'] >= min_pts]
            if len(sizes) == 0:
                continue
            first = sizes.sort_values(ct).iloc[0]
            sel = pd.Series(True, index=wdf.index)
            for k in gk:
                sel &= (wdf[k] == first[k])
            g = wdf[sel]
        else:
            if len(wdf) < min_pts:
                continue
            g = wdf
        if len(g) == 0:
            continue
        fig, ax = plt.subplots(figsize=(size_in, size_in))
        ax.scatter(g[cx].astype(float), g[cy].astype(float), c=g[item].astype(float),
                   cmap=cmap, norm=norm, s=s, marker='s', linewidths=0)
        ax.set_xticks([]); ax.set_yticks([]); ax.set_aspect('equal', adjustable='box'); ax.set_facecolor('#f8f9fa')
        ax.set_xlim(_gx0 - _xpad, _gx1 + _xpad)
        ax.set_ylim(_gy0 - _ypad, _gy1 + _ypad)
        for sp in ax.spines.values():
            sp.set_visible(False)
        fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', pad_inches=0.02, facecolor='white')
        plt.close(fig)
        try:
            _wkey = str(int(wid))
        except (ValueError, TypeError):
            _wkey = str(wid)
        if by_lot and lot_val is not None:
            out[f"{lot_val}|{_wkey}"] = base64.b64encode(buf.getvalue()).decode('utf-8')
        else:
            out[_wkey] = base64.b64encode(buf.getvalue()).decode('utf-8')
    return out


def render_specout_wfmaps_b64(merged_df, item, spec_low=None, spec_high=None,
                              target_lot=None, max_maps=25,
                              size_in=0.62, dpi=None):
    """spec-out(=flier) 칩맵을 측정(lot, wafer, tkout) 단위로 그려 [(label, b64), ...]로 반환.

    [0] Anomaly Trend Chart 의 SPEC OUT 항목 우측에 붙이는 용도.
      - 칩 색: spec 통과=회색(#bdbdbd), spec 이탈=빨강(#d32f2f).
      - 대상: spec-out 칩이 1개 이상 있는 측정(tkout)만.
      - 정렬/상한: ① target_lot(FAB_LOT_ID)의 spec-out wafer는 wafer_id 오름차순으로 '모두' 표시
                   (lot 25매가 다 spec이면 25장 다 나옴 — max_maps와 무관하게 전량 보장),
                   ② 남는 칸은 다른 lot을 TKOUT_TIME 최신순으로 max_maps 총개수까지 채움.
      - label = f"{ROOT_LOT_ID} #{WAFER_ID}".
    spec_low/spec_high는 호출부에서 REPORT DIRECTION을 이미 반영한 값(UPPER=하한 None,
    LOWER=상한 None)을 넘겨야 Trend의 SPEC OUT 판정과 일치한다.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import base64, io

    if dpi is None:   # HTML용 WF MAP 해상도는 My_config.html_wfmap_dpi로 관리
        dpi = getattr(GLOBAL_CONFIG, 'html_wfmap_dpi', 110)

    if item not in merged_df.columns:
        return []
    if spec_low is None and spec_high is None:
        return []

    def _pick(*names):
        for n in names:
            if n in merged_df.columns:
                return n
        return None
    cx = _pick('CHIP_X_ADJ', 'CHIP_X_POS', 'chip_x_pos')
    cy = _pick('CHIP_Y_ADJ', 'CHIP_Y_POS', 'chip_y_pos')
    cw = _pick('WAFER_ID', 'wafer_id')
    ct = _pick('TKOUT_TIME', 'tkout_time')
    croot = _pick('ROOT_LOT_ID', 'root_lot_id')
    clot = _pick('FAB_LOT_ID', 'fab_lot_id')
    if not (cx and cy and cw and ct):
        return []

    keep = [c for c in [cx, cy, cw, ct, croot, clot, item] if c]
    d = merged_df[keep].dropna(subset=[item]).copy()
    if len(d) == 0:
        return []
    d[ct] = pd.to_datetime(d[ct], errors='coerce')
    vals = pd.to_numeric(d[item], errors='coerce')
    lo = None if spec_low is None else float(spec_low)
    hi = None if spec_high is None else float(spec_high)
    out_mask = pd.Series(False, index=d.index)
    if lo is not None:
        out_mask = out_mask | (vals < lo)
    if hi is not None:
        out_mask = out_mask | (vals > hi)
    d['_specout'] = out_mask.values

    # 측정 단위 그룹: (root_lot, fab_lot, wafer, tkout) — spec-out 칩이 있는 측정만
    gcols = [c for c in [croot, clot, cw, ct] if c]
    groups = []
    for gkey, g in d.groupby(gcols, dropna=False):
        if not g['_specout'].any():
            continue
        gvals = gkey if isinstance(gkey, tuple) else (gkey,)
        gd = dict(zip(gcols, gvals))
        groups.append((gd, g))
    if not groups:
        return []

    def _is_tgt(gd):
        return target_lot is not None and clot is not None and str(gd.get(clot)) == str(target_lot)

    def _waf(gd):
        try:
            return int(gd.get(cw))
        except (ValueError, TypeError):
            return 10 ** 9
    tgt = sorted([x for x in groups if _is_tgt(x[0])], key=lambda x: _waf(x[0]))
    rest = sorted([x for x in groups if not _is_tgt(x[0])],
                  key=lambda x: (x[0].get(ct) if pd.notna(x[0].get(ct)) else pd.Timestamp.min),
                  reverse=True)
    # target lot의 spec-out wafer는 max_maps와 무관하게 전량 표시, 남는 칸만 나머지로 채움
    if max_maps and max_maps > 0:
        _n_rest = max(0, int(max_maps) - len(tgt))
        ordered = tgt + rest[:_n_rest]
    else:
        ordered = tgt + rest

    gdim = max(int(d[cx].nunique()), int(d[cy].nunique()), 1)
    chip_pt = size_in / gdim * 72.0
    s = max(1.0, (chip_pt * 0.9) ** 2)
    # 전체 wafer 격자가 잘리지 않도록 공통 축범위 + 반칩 여백 (모든 소형맵 동일 범위)
    gx0, gx1 = float(d[cx].min()), float(d[cx].max())
    gy0, gy1 = float(d[cy].min()), float(d[cy].max())
    xr = (gx1 - gx0) or 1.0; yr = (gy1 - gy0) or 1.0
    xpad = xr / max(int(d[cx].nunique()) - 1, 1) * 0.7 if d[cx].nunique() > 1 else xr * 0.1
    ypad = yr / max(int(d[cy].nunique()) - 1, 1) * 0.7 if d[cy].nunique() > 1 else yr * 0.1

    res = []
    for gd, g in ordered:
        fig, ax = plt.subplots(figsize=(size_in, size_in))
        colors = np.where(g['_specout'].values, '#d32f2f', '#bdbdbd')
        ax.scatter(g[cx].astype(float), g[cy].astype(float), c=colors,
                   s=s, marker='s', linewidths=0)
        ax.set_xticks([]); ax.set_yticks([]); ax.set_aspect('equal', adjustable='box'); ax.set_facecolor('#f8f9fa')
        ax.set_xlim(gx0 - xpad, gx1 + xpad)
        ax.set_ylim(gy0 - ypad, gy1 + ypad)
        for sp in ax.spines.values():
            sp.set_visible(False)
        fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', pad_inches=0.02, facecolor='white')
        plt.close(fig)
        _root = gd.get(croot, '') if croot else ''
        _wv = gd.get(cw, '')
        try:
            _wv = int(_wv)
        except (ValueError, TypeError):
            pass
        res.append((f"{_root} #{_wv}", base64.b64encode(buf.getvalue()).decode('utf-8')))
    return res


def _wfmap_cmap(direction):
    """REPORT DIRECTION별 WF MAP 컬러맵.
      - LOWER(하한 관리): 낮은값=빨강, 높은값=파랑  → 'coolwarm_r'
      - UPPER/BOTH:       낮은값=파랑, 높은값=빨강  → 'coolwarm'
    """
    return 'coolwarm_r' if str(direction).strip().upper() == 'LOWER' else 'coolwarm'


def _wfmap_norm(direction, spec_low, spec_high, values):
    """WF MAP diverging 컬러 normalization (PPT/HTML 공통, 동일 규칙).

    spec line 기준으로 색을 고정한다:
      - speclow → 컬러맵 0(파랑끝) · spechigh → 1(빨강끝) · median → 0.5(연회색 center)
      - cmap(_wfmap_cmap)이 LOWER에서 반전되므로 색의 빨강/파랑은 방향에 맞게 자동 적용
        (UPPER/BOTH: spechigh=빨강·speclow=파랑 / LOWER: 반대).
    한쪽 spec만 있으면(UPPER/LOWER) median 기준 대칭 확장하고, 둘 다 없으면 데이터 1~99% 선형 fallback.
    """
    import numpy as np
    import matplotlib.colors as mcolors

    def _f(x):
        try:
            x = float(x)
            return x if np.isfinite(x) else None
        except Exception:
            return None

    v = pd.to_numeric(pd.Series(values), errors='coerce').dropna().astype(float)
    med = float(v.median()) if len(v) else None
    lo, hi = _f(spec_low), _f(spec_high)
    if med is not None:
        if lo is None and hi is not None:
            lo = med - (hi - med)
        elif hi is None and lo is not None:
            hi = med + (med - lo)
    if med is None or lo is None or hi is None or not (hi > lo):
        # spec 결손 → 데이터 1~99% 선형
        if len(v):
            vmin, vmax = float(v.quantile(0.01)), float(v.quantile(0.99))
        else:
            vmin, vmax = 0.0, 1.0
        if not (vmax > vmin):
            vmax = vmin + 1e-9
        return mcolors.Normalize(vmin=vmin, vmax=vmax)
    # median(연회색 center)이 [lo,hi] 안에 오도록 clamp (TwoSlopeNorm 요구: lo<vc<hi)
    eps = (hi - lo) * 1e-6
    vc = min(max(med, lo + eps), hi - eps)
    return mcolors.TwoSlopeNorm(vmin=lo, vcenter=vc, vmax=hi)


def render_index_wfmap_b64(df, item, min_pts=50, lot_prefix=None,
                           cmap=None, size_in=1.15, dpi=None, direction='BOTH'):
    """특정 index의 '첫 측정(가장 이른 tkout_time) WF MAP'을 base64 PNG로 반환.

    - min_pts 이상 측정된 (lot, wafer, tkout) 그룹만 대상으로 하고, 그 중 tkout_time이
      가장 이른 그룹(=첫 측정)의 단일 wafer 칩맵을 그린다.
    - 조건을 만족하는 측정이 없으면 None (Score Board에 썸네일 미표기).
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import base64, io

    if dpi is None:   # HTML용 WF MAP 해상도는 My_config.html_wfmap_dpi로 관리
        dpi = getattr(GLOBAL_CONFIG, 'html_wfmap_dpi', 120)

    if item not in df.columns:
        return None

    def _pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None
    cx = _pick('CHIP_X_ADJ', 'CHIP_X_POS', 'chip_x_pos')
    cy = _pick('CHIP_Y_ADJ', 'CHIP_Y_POS', 'chip_y_pos')
    cw = _pick('WAFER_ID', 'wafer_id')
    ct = _pick('TKOUT_TIME', 'tkout_time')
    cl = _pick('FAB_LOT_ID', 'fab_lot_id')
    if not (cx and cy and cw and ct):
        return None

    cols = [c for c in [cx, cy, cw, ct, cl, item] if c]
    d = df[cols].dropna(subset=[item]).copy()
    if lot_prefix and cl:
        d = d[d[cl].astype(str).str.startswith(str(lot_prefix))]
    if len(d) == 0:
        return None
    d[ct] = pd.to_datetime(d[ct], errors='coerce')

    grp_keys = [k for k in [cl, cw, ct] if k]
    sizes = d.groupby(grp_keys).size().reset_index(name='n')
    sizes = sizes[sizes['n'] >= min_pts]
    if len(sizes) == 0:
        return None
    first = sizes.sort_values(ct).iloc[0]          # 가장 이른 tkout 그룹
    sel = pd.Series(True, index=d.index)
    for k in grp_keys:
        sel &= (d[k] == first[k])
    g = d[sel]
    if len(g) == 0:
        return None

    vals = d[item].astype(float)
    vmin = float(vals.quantile(0.01)); vmax = float(vals.quantile(0.99))
    if not (vmax > vmin):
        vmax = vmin + 1e-9
    gdim = max(int(d[cx].nunique()), int(d[cy].nunique()), 1)
    chip_pt = size_in / gdim * 72.0
    s = max(1.0, (chip_pt * 0.9) ** 2)

    fig, ax = plt.subplots(figsize=(size_in, size_in))
    ax.scatter(g[cx].astype(float), g[cy].astype(float), c=g[item].astype(float),
               cmap=cmap or _wfmap_cmap(direction), vmin=vmin, vmax=vmax,
               s=s, marker='s', linewidths=0)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_aspect('equal')
    ax.set_facecolor('#f8f9fa')
    for sp in ax.spines.values():
        sp.set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def calcaulate_description_image_info_dict(description_ppt_path, img_quality=20):
    """설명 PPT(python-pptx)를 열어 슬라이드별 좌상단 텍스트(Category)→슬라이드 매핑 반환.

    PNG 변환/중간파일(win32com Export) 의존을 제거했다. HOL_Auto_Report_Description.pptx
    파일만 있으면 되고, 이후 insert_plots가 매칭된 슬라이드를 python-pptx로 **직접 복사**해
    삽입한다. (PowerPoint 설치·COM 불필요, RUN/TEMP/desc_slide_*.png 미생성)

    반환: {category_text_lower: source_slide_object}. source_slide는 원본 Presentation을
    참조하므로, 반환 dict가 살아있는 동안 원본 패키지도 유지된다.
    """
    from pptx import Presentation

    desc_dict = {}
    if not description_ppt_path or not os.path.exists(description_ppt_path):
        print(f"[WARN] Description PPT를 찾을 수 없습니다: {description_ppt_path}")
        return desc_dict

    try:
        src_prs = Presentation(description_ppt_path)
        for slide in src_prs.slides:
            best_text, min_dist = None, float('inf')
            # 슬라이드 내 모든 도형 스캔 후 (0,0) 좌상단에 가장 가까운 텍스트를 Category로
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                text = (shape.text_frame.text or "").strip()
                if not text:
                    continue
                top = shape.top if shape.top is not None else 0
                left = shape.left if shape.left is not None else 0
                dist = int(top) ** 2 + int(left) ** 2
                if dist < min_dist:
                    min_dist = dist
                    best_text = text.split('\r')[0].split('\n')[0].strip()
            if best_text:
                key = best_text.replace('\x0b', '').strip().lower()
                if key:
                    desc_dict[key] = slide
    except Exception as e:
        print(f"[ERROR] Description PPT 파싱 중 에러 발생: {e}")

    return desc_dict


def _copy_slide_into(dest_prs, src_slide):
    """src_slide(다른 Presentation의 슬라이드)를 dest_prs에 새 슬라이드로 직접 복사.

    python-pptx로 도형 XML을 복사하고, 이미지 등 관계(rel)를 dest로 옮기며 rId를
    재매핑한다. PNG 변환 없이 원본 슬라이드 내용을 그대로 삽입한다.
    """
    import copy as _copy
    _R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

    dest_slide = dest_prs.slides.add_slide(dest_prs.slide_layouts[6])
    # 빈 레이아웃의 기본 placeholder 제거(빈 상자 방지)
    for ph in list(dest_slide.shapes):
        try:
            if ph.is_placeholder:
                ph._element.getparent().remove(ph._element)
        except Exception:
            pass

    # 관계(이미지 등) 복사 → old rId → new rId 매핑 (레이아웃/노트 관계는 제외)
    rid_map = {}
    try:
        for rId, rel in src_slide.part.rels.items():
            if 'slideLayout' in rel.reltype or 'notesSlide' in rel.reltype:
                continue
            if rel.is_external:
                new_rId = dest_slide.part.rels.get_or_add_ext_rel(rel.reltype, rel._target)
            else:
                new_rId = dest_slide.part.relate_to(rel.target_part, rel.reltype)
            rid_map[rId] = new_rId
    except Exception as _re:
        print(f"[WARN] description 슬라이드 관계 복사 일부 실패: {_re}")

    # 도형 XML 복사
    for shape in src_slide.shapes:
        dest_slide.shapes._spTree.append(_copy.deepcopy(shape._element))

    # 복사된 XML의 old rId 참조를 new rId로 치환 (r:embed, r:link, r:id 등)
    if rid_map:
        for el in dest_slide.shapes._spTree.iter():
            for attr in list(el.attrib):
                if attr.startswith(_R) and el.attrib[attr] in rid_map:
                    el.attrib[attr] = rid_map[el.attrib[attr]]
    return dest_slide


def insert_plots(merged_df, prs, description_image_info_dict,
                 target_lot_id, target_root_lot_id,
                 target_DC_step, target_DC_step_id,
                 spec_data, img_quality=12, ref=False, reformatter=None, dpi=None):
    """최고급 다중 차트 및 통계표 대시보드를 생성하여 PPT에 삽입.

    각 측정 항목(item)에 대해 하나의 슬라이드를 생성하며, 슬라이드 구성:
        - 좌측 상단: 통계 테이블 (Mean/Std/Min/Max, 전체 + 웨이퍼별)
        - 좌측 중앙: BOX Plot (웨이퍼별 분포)
        - 좌측 하단: WF MAP (PGM × Wafer 산점도)
        - 우측 상단: Trend Chart (시계열 + 3일 구름대)
        - 우측 중앙: Radius Plot (반경별 분포 + 3차 근사)
        - 우측 하단: Cumulative Plot (누적 분포)
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    import gc
    import numpy as np
    import shutil
    import re

    # ---- 차트 해상도/압축 품질 설정 (My_config.py에서 조정) ----
    # dpi가 명시되지 않으면 GLOBAL_CONFIG.ppt_chart_dpi 사용 (엔지니어가 직접 변경 가능)
    if dpi is None:
        dpi = GLOBAL_CONFIG.ppt_chart_dpi
    jpg_q = GLOBAL_CONFIG.ppt_chart_jpg_quality   # 라인/박스/트렌드/CDF/레전드 JPEG 품질
    map_q = GLOBAL_CONFIG.ppt_map_jpg_quality     # WF MAP JPEG 품질
    # 차트 공통 색 팔레트 (config에서 단일 관리)
    C_NAVY = GLOBAL_CONFIG.chart_navy
    C_ACCENT = GLOBAL_CONFIG.chart_accent
    C_NEUTRAL = GLOBAL_CONFIG.chart_neutral
    C_GRID = GLOBAL_CONFIG.chart_grid
    C_SPINE = GLOBAL_CONFIG.chart_spine
    C_VEHICLE = GLOBAL_CONFIG.chart_vehicle          # main vehicle scatter (초록)
    C_WV = GLOBAL_CONFIG.chart_with_vehicle          # with_vehicle scatter (회색, 단일 fallback)
    # with_vehicle이 여러 개일 때 각각 다른 색으로 구분하기 위한 팔레트
    # (첫번째는 기존 회색 유지, 이후 항목은 구분되는 색)
    WV_PALETTE = [C_WV, '#9467bd', '#8c564b', '#17becf', '#bcbd22', '#e377c2', '#ff7f0e', '#1f77b4']
    C_BAND = GLOBAL_CONFIG.chart_band                # vehicle 1~99% 구름대 (연초록)
    FONT = GLOBAL_CONFIG.theme_font_family
    NAVY_RGB = GLOBAL_CONFIG.theme_title_color    # (R,G,B) 헤더/타이틀 통일 색
    main_vehicle = GLOBAL_CONFIG.get('vehicle')   # mask 비교 기준 (main vehicle 이름)

    # --- 컬럼명 대소문자 방어 (Column Name Case-Insensitive Lookup) ---
    def _pick(*names):
        for n in names:
            if n in merged_df.columns:
                return n
        return None
    col_x = _pick('chip_x_pos', 'CHIP_X_POS')
    col_y = _pick('chip_y_pos', 'CHIP_Y_POS')
    col_rad = _pick('chip_radius', 'Chip_Radius')
    col_sub = _pick('subitem_id', 'SUBITEM_ID')
    col_time = _pick('tkout_time', 'TKOUT_TIME')   # merged_df는 대문자(TKOUT_TIME)일 수 있음
    col_lot = _pick('fab_lot_id', 'FAB_LOT_ID')
    col_mask = _pick('mask', 'MASK')

    # 고정된 25매 웨이퍼 리스트
    fixed_wafers = [str(i) for i in range(1, 26)]

    # ── Wafer ID별 고정 컬러 팔레트 (25색, 시각적으로 잘 구분되는 색) ──
    # wafer #1 → WAFER_PALETTE[0], #2 → [1] ... 처럼 wafer 번호를 고정 색에 매핑합니다.
    # 따라서 동일 wafer 번호는 모든 차트/리포트에서 항상 같은 색으로 표시됩니다.
    # (Legend / Box / Trend / Radius / CDF 전부 이 w_colors를 공통 사용)
    WAFER_PALETTE = [
        '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
        '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990',
        '#dcbeff', '#9a6324', '#bcbd22', '#800000', '#aaffc3',
        '#808000', '#ffd8b1', '#000075', '#a9a9a9', '#ffe119',
        '#1a1aff', '#ff4dd2', '#00cca3', '#b35900', '#5c5c8a',
    ]
    w_colors = {str(i): WAFER_PALETTE[(i - 1) % len(WAFER_PALETTE)] for i in range(1, 26)}

    current_cat = None
    metrics_dict = {}

    import os
    os.makedirs("RUN/TEMP", exist_ok=True)

    def _remove_spines(ax):
        for spine in ['top', 'right']:
            ax.spines[spine].set_visible(False)
        ax.spines['left'].set_color(C_SPINE)
        ax.spines['bottom'].set_color(C_SPINE)
        ax.spines['left'].set_linewidth(0.6)
        ax.spines['bottom'].set_linewidth(0.6)

    # 대상 lot 선택 헬퍼: fab_lot_id 정확일치 → 실패 시 root_lot prefix.
    # (전체 vehicle로 확장하지 않음 — Box/Radius/CDF/Trend의 target은 해당 lot만)
    def _select_target_lot(frame):
        # 리포트 단위 = root_lot_id + step (match_key). 같은 root의 형제 lot_id를 모두 포함한다.
        if 'match_key' in frame.columns and target_root_lot_id and target_DC_step_id:
            mk = f"{target_root_lot_id}_{target_DC_step_id}"
            sel = frame[frame['match_key'].astype(str) == mk]
            if len(sel) > 0:
                return sel
        if 'fab_lot_id' not in frame.columns:
            return frame
        flid = frame['fab_lot_id'].astype(str)
        sel = frame[flid == str(target_lot_id)]
        if len(sel) == 0 and target_root_lot_id:
            sel = frame[flid.str.startswith(str(target_root_lot_id))]
        return sel

    # --- 페이지 index 목록 구성 (spec_data 항목 + Reformatize 파생 항목 포함) ---
    # Reformatize는 ADDP FORM으로 단일/다중 컬럼 파생 index를 만든다.
    #  - 단일컬럼 파생(예: VTH_AVG)  → merged_df에 'VTH_AVG' 컬럼 그대로 존재
    #  - 다중컬럼 파생(예: window)   → 'WINDOW_ovl_index', 'WINDOW_new' 등 접두어_컬럼으로 존재
    # 따라서 spec_data의 각 ALIAS에 대해, 그 ALIAS로 시작하는 모든 merged_df 컬럼을
    # 페이지 대상에 포함시켜 파생 항목도 빠짐없이 PPT 페이지가 생성되도록 한다.
    # plot_items: (데이터 컬럼명, spec 메타 출처 ALIAS)
    # REPORT ORDER 오름차순(작은 값 먼저)으로 index 정렬
    if 'REPORT ORDER' in spec_data.columns:
        _ro = pd.to_numeric(spec_data['REPORT ORDER'], errors='coerce')
        ordered_index = _ro.sort_values(kind='stable').index
    else:
        ordered_index = spec_data.index
    plot_items = []
    seen_cols = set()
    for nm in ordered_index:
        # alias 자체가 컬럼이면 포함(MA_Window의 '' 출력 = alias) + alias_로 시작하는 다중컬럼 파생 모두 포함
        cols_for_nm = []
        if nm in merged_df.columns:
            cols_for_nm.append(nm)
        cols_for_nm += [c for c in merged_df.columns if str(c).startswith(str(nm) + "_")]
        if cols_for_nm:
            for d in cols_for_nm:
                if d not in seen_cols:
                    plot_items.append((d, nm))
                    seen_cols.add(d)
        else:
            plot_items.append((nm, nm))  # 데이터 없음 → 루프에서 skip

    # --- 항목별 슬라이드 생성 루프 (Per-Item Slide Generation Loop) ---
    total_items = len(plot_items)
    summary_rows = []   # 마지막 summary 페이지용 (index별 REPORT DIRECTION 기준 집계값)
    print("=" * 60)
    print(f"[insert_plots] 차트 생성 시작 - 총 {total_items}개 index(파생 포함) 처리 예정")
    print("=" * 60)
    for idx, (item_name, spec_name) in enumerate(plot_items, start=1):
        if item_name not in merged_df.columns:
            print(f"[{idx}/{total_items}] {item_name} 건너뜀 (merged_df에 데이터 없음)")
            continue
        print(f"[{idx}/{total_items}] {item_name} 처리 중...")

        # 파일명 안전화: index명에 '/' 등 경로/금지문자가 있으면 '_'로 치환
        # (예: 'IDSAT_N/IDSAT_P' → 'IDSAT_N_IDSAT_P') → 저장 시 에러 방지
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', str(item_name))

        # ---- 카테고리 간지(Description) 슬라이드 삽입 ----
        if 'CAT2' in spec_data.columns:
            cat2 = str(spec_data.loc[spec_name, 'CAT2']).strip()
            if cat2 != current_cat and cat2.lower() != 'nan':
                current_cat = cat2
                matched_slide = None
                ck = cat2.lower().strip()
                # 1) CAT2 글자와 정확히 일치하는 description 슬라이드 우선
                for key, _src_slide in description_image_info_dict.items():
                    if key.lower().strip() == ck:
                        matched_slide = _src_slide
                        break
                # 2) 없으면 CAT2 글자가 포함(부분일치)된 description 슬라이드 탐색
                if matched_slide is None and ck:
                    for key, _src_slide in description_image_info_dict.items():
                        kl = key.lower().strip()
                        if ck in kl or kl in ck:
                            matched_slide = _src_slide
                            break

                # CAT2 그룹 시작 전에 description 슬라이드를 python-pptx로 직접 복사해 삽입 (PNG 변환 없음)
                if matched_slide is not None:
                    try:
                        _copy_slide_into(prs, matched_slide)
                    except Exception as _de:
                        print(f"[WARN] CAT2 '{cat2}' description 슬라이드 복사 실패: {_de}")
                else:
                    print(f"[WARN] CAT2 '{cat2}' description 슬라이드를 찾지 못해 간지 생략")

        try:
            # ---- 데이터 준비 (Data Preparation) ----
            w_col_src = _pick('wafer_id', 'WAFER_ID') or 'wafer_id'
            req_cols = [w_col_src]
            if col_x: req_cols += [col_x, col_y]
            if col_rad: req_cols.append(col_rad)
            if col_time: req_cols.append(col_time)
            if col_sub: req_cols.append(col_sub)
            if col_lot: req_cols.append(col_lot)
            if col_mask: req_cols.append(col_mask)
            if 'CHIP_X_ADJ' in merged_df.columns: req_cols.append('CHIP_X_ADJ')
            if 'CHIP_Y_ADJ' in merged_df.columns: req_cols.append('CHIP_Y_ADJ')
            if 'match_key' in merged_df.columns: req_cols.append('match_key')  # 형제 lot 묶음(root+step) 선택용
            if 'PGM(pt)' in merged_df.columns: req_cols.append('PGM(pt)')      # WF MAP 행 좌측 PGM(pt) 라벨용

            item_df = merged_df[list(set(req_cols)) + [item_name]].dropna(subset=[item_name]).copy()
            if len(item_df) == 0:
                continue

            # 메타 컬럼명을 canonical 소문자로 통일 (merged_df가 대문자여도 안전)
            rename_map = {}
            if col_time and col_time != 'tkout_time': rename_map[col_time] = 'tkout_time'
            if col_lot and col_lot != 'fab_lot_id': rename_map[col_lot] = 'fab_lot_id'
            if col_mask and col_mask != 'mask': rename_map[col_mask] = 'mask'
            if w_col_src != 'wafer_id': rename_map[w_col_src] = 'wafer_id'
            if rename_map:
                item_df = item_df.rename(columns=rename_map)
            w_col = 'wafer_id'

            # 누락 컬럼 대체 (Fallback for missing columns)
            if not col_x:
                item_df['chip_x_pos'] = np.random.rand(len(item_df))
                item_df['chip_y_pos'] = np.random.rand(len(item_df))
                col_x, col_y = 'chip_x_pos', 'chip_y_pos'
            if not col_rad:
                item_df['chip_radius'] = np.sqrt(item_df[col_x]**2 + item_df[col_y]**2)
                col_rad = 'chip_radius'
            if not col_sub:
                item_df['subitem_id'] = 'ALL'
                col_sub = 'subitem_id'
            if 'tkout_time' not in item_df.columns:
                item_df['tkout_time'] = pd.to_datetime('today')

            item_df[w_col] = item_df[w_col].astype(str).str.replace('#', '')
            item_df['tkout_time'] = pd.to_datetime(item_df['tkout_time'])
            item_df = item_df.sort_values(by='tkout_time')

            # ---- 데이터 스코프 분리 ----
            # item_df_full: 전 vehicle·전 lot (Trend 비교 전용)
            # item_df(=target_df): 리포팅 대상 lot만 (Box/WF MAP/통계표/Radius/CDF/통계)
            # NOTE: 대상 lot 매칭 실패 시에도 전체 vehicle 데이터로 확장하지 않는다.
            #       (Box/Radius/CDF는 반드시 해당 lot의 wafer만 표시되어야 함)
            item_df_full = item_df
            target_df = _select_target_lot(item_df)
            item_df = target_df
            if len(item_df) == 0:
                print(f"[{idx}/{total_items}] {item_name} 건너뜀 (대상 lot '{target_lot_id}' 데이터 없음)")
                continue

            measured_wafers = sorted(item_df[w_col].unique(), key=lambda x: int(x) if str(x).isdigit() else x)
            grouped = item_df.groupby(w_col)[item_name]

            # ---- 다중 lot_id 구분 (같은 root_lot_id에 여러 lot_id가 묶여 함께 reporting될 때) ----
            #  - Radius/CDF: lot_id별 marker 모양으로 구분 (wafer 색은 유지)
            #  - Trend: lot_id별 색으로 구분
            lot_col = 'fab_lot_id' if 'fab_lot_id' in item_df.columns else None
            target_lots = (sorted(item_df[lot_col].astype(str).unique()) if lot_col else [])
            multi_lot = len(target_lots) > 1
            LOT_MARKERS = ['o', '^', 's', 'D', 'v', 'P', 'X', '*']
            LOT_LINE_COLORS = ['#d62728', '#1f77b4', '#2ca02c', '#9467bd', '#ff7f0e', '#8c564b', '#e377c2', '#17becf']
            lot_marker = {lot: LOT_MARKERS[i % len(LOT_MARKERS)] for i, lot in enumerate(target_lots)}
            lot_color = {lot: LOT_LINE_COLORS[i % len(LOT_LINE_COLORS)] for i, lot in enumerate(target_lots)}

            # ---- 스펙 범위 추출 (Spec Limits) + 방향/로그/단위 ----
            spec_low = spec_data.loc[spec_name, "SPECLOW"]
            spec_high = spec_data.loc[spec_name, "SPECHIGH"]
            if pd.isna(spec_low): spec_low = None
            if pd.isna(spec_high): spec_high = None
            # WF MAP 컬러 anchor용 원본 spec(방향 nulling 전): speclow/spechigh 양끝 모두 보존
            wfmap_spec_low, wfmap_spec_high = spec_low, spec_high

            # REPORT DIRECTION: UPPER=상한만, LOWER=하한만, BOTH=둘 다
            direction = 'BOTH'
            if 'REPORT DIRECTION' in spec_data.columns:
                _d = str(spec_data.loc[spec_name, 'REPORT DIRECTION']).strip().upper()
                if _d in ('UPPER', 'LOWER', 'BOTH'):
                    direction = _d
            if direction == 'UPPER':
                spec_low = None
            elif direction == 'LOWER':
                spec_high = None

            # REPORT LOG SCALE: 값 축 log10 적용 여부
            log_scale = False
            if 'REPORT LOG SCALE' in spec_data.columns:
                _ls = spec_data.loc[spec_name, 'REPORT LOG SCALE']
                log_scale = str(_ls).strip().lower() in ('true', '1', '1.0', 'yes')

            # UNIT: 축 라벨 단위
            unit = ''
            if 'UNIT' in spec_data.columns:
                _u = str(spec_data.loc[spec_name, 'UNIT']).strip()
                if _u and _u.lower() != 'nan':
                    unit = _u
            y_label = f"{item_name} [{unit}]" if unit else item_name

            # ---- Index Aggregation Table용 (lot, wafer)별 집계값 ----
            # REPORT DIRECTION → BOTH=Median / UPPER=P90 / LOWER=P10. lot_id별로 분리해 wafer별 1값.
            try:
                if direction == 'UPPER':
                    _q, _sstat = 0.90, 'P90'
                elif direction == 'LOWER':
                    _q, _sstat = 0.10, 'P10'
                else:
                    _q, _sstat = None, 'Median'

                def _aggv(s):
                    s = pd.to_numeric(s, errors='coerce').dropna()
                    if len(s) == 0:
                        return None
                    return float(s.median()) if _q is None else float(s.quantile(_q))

                _lc = 'fab_lot_id' if 'fab_lot_id' in item_df.columns else None
                _wv = {}   # {(lot, wafer_int): value}
                if _lc:
                    for (_lot, _waf), _g in item_df.groupby([_lc, w_col]):
                        _v = _aggv(_g[item_name])
                        if _v is None:
                            continue
                        try:
                            _waf = int(float(_waf))
                        except (ValueError, TypeError):
                            pass
                        _wv[(str(_lot), _waf)] = _v
                else:
                    _v = _aggv(item_df[item_name])
                    if _v is not None:
                        _wv[(str(target_lot_id), 0)] = _v
                if _wv:
                    summary_rows.append({'index': item_name, 'stat': _sstat, 'wafer_vals': _wv})
            except Exception as _se:
                print(f"[WARN] summary 집계 실패 ({item_name}): {_se}")

            slide = prs.slides.add_slide(prs.slide_layouts[6])
            
            # 슬라이드 배경색 설정 (흰색)
            slide_bg = slide.background
            slide_bg.fill.solid()
            slide_bg.fill.fore_color.rgb = RGBColor(255, 255, 255)
            
            # ---- 레이아웃 상수 (와이드 16:9 13.333x7.5, 여백 최소화·꽉 채움) ----
            # 좌/우 2열을 슬라이드 폭(13.333")에 꽉 차게 배치, 여백 최소화
            LX, RX = 0.12, 8.50          # 좌/우 열 X 좌표 (테이블·그림·타이틀 공통)
            LW, RW = 8.30, 4.70          # 좌/우 열 폭 (LX+LW=8.42, RX+RW=13.20)
            TITLE_GAP = 0.22             # 타이틀과 바로 아래 그림 사이 간격
            SLIDE_BOTTOM = 7.40          # 차트가 채울 슬라이드 하단 경계 (7.5" - 하단여백)
            # 그림 top Y 좌표 (좌열: 통계표/레전드/Box/WFMAP, 우열: Trend/Radius/CDF)
            Y_TABLE, Y_LEG, Y_BOX, Y_MAP = 0.68, 2.00, 2.58, 4.92
            Y_TREND, Y_RAD, Y_CUM = 0.92, 3.08, 5.24

            # 타이틀 카드 타이틀 생성 헬퍼 함수 (그림 top - TITLE_GAP 위치에 배치)
            def add_card_title(text, l, pic_top):
                tx = slide.shapes.add_textbox(Inches(l), Inches(pic_top - TITLE_GAP), Inches(RW), Inches(0.3))
                tf = tx.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                p.text = text
                p.font.size = Pt(10.5)
                p.font.bold = True
                p.font.color.rgb = RGBColor(*NAVY_RGB)
                p.font.name = FONT

            # INDEX 옆에 관련 REAL ITEM 이름 파싱해서 기입
            real_items_str = ""
            if reformatter is not None and spec_name in reformatter['ALIAS'].values:
                row = reformatter[reformatter['ALIAS'] == spec_name].iloc[0]
                cat = row.get('CATEGORY', '')
                if cat == 'REAL':
                    real_items_str = str(row.get('ITEMID', ''))
                elif cat == 'ADDP':
                    formula = str(row.get('ADDP FORM', ''))
                    items = re.findall(r'[A-Za-z0-9_]+', formula)
                    items = [it for it in items if not it.isdigit()]
                    real_items_str = ", ".join(sorted(list(set(items))))

            # ---- 1. HEADER & INDEX NAME (Title) ----
            header_shape = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.58))
            header_shape.fill.solid()
            header_shape.fill.fore_color.rgb = RGBColor(*NAVY_RGB)
            header_shape.line.fill.background()

            txBox = slide.shapes.add_textbox(Inches(0.2), Inches(0.06), Inches(12.9), Inches(0.45))
            tf = txBox.text_frame
            p = tf.paragraphs[0]
            p.text = f" {item_name}"
            p.font.size = Pt(22)
            p.font.bold = True
            p.font.color.rgb = RGBColor(255, 255, 255)
            p.font.name = FONT

            if real_items_str:
                run = p.add_run()
                run.text = f"  ({real_items_str})"
                run.font.size = Pt(11)
                run.font.bold = False
                run.font.color.rgb = RGBColor(200, 208, 224) # 연회색 소프트 폰트
                run.font.name = FONT

            # 네이티브 타이틀 추가 (각 그림 top 좌표 기준으로 정렬)
            add_card_title("■ Wafer-level Distribution (Box Plot)", LX, Y_BOX)
            add_card_title("■ WF MAP", LX, Y_MAP)
            add_card_title("■ Trend", RX, Y_TREND)
            add_card_title("■ Radius plot", RX, Y_RAD)
            add_card_title("■ Cumulative plot", RX, Y_CUM)

            # ---- 2. Statistical Table (통계 테이블: #1~25 고정 컬럼, Total 열 없음) ----
            cols = 26 # Stat + #1 ~ #25 고정 (Total 열 제거)
            table_shape = slide.shapes.add_table(5, cols, Inches(LX), Inches(Y_TABLE), Inches(LW), Inches(1.2)).table

            # 헤더
            headers = ["Stat"] + [f"#{w}" for w in range(1, 26)]
            for c_idx, h in enumerate(headers):
                cell = table_shape.cell(0, c_idx)
                cell.text = h
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor(*NAVY_RGB) # 네이비 헤더 (통일)
                cell.margin_left = Inches(0.0)
                cell.margin_right = Inches(0.0)
                cell.margin_top = Inches(0.0)
                cell.margin_bottom = Inches(0.0)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.text_frame.word_wrap = False
                for par in cell.text_frame.paragraphs:
                    par.font.color.rgb = RGBColor(255, 255, 255)
                    par.font.bold = True
                    par.font.size = Pt(7)
                    par.font.name = FONT
                    par.alignment = PP_ALIGN.CENTER
            
            # REPORT DIRECTION별 통계 행 구성
            #   UPPER: Max P90 Med P10 / LOWER: P90 Med P10 Min / BOTH: P90 Med P10 Std
            if direction == 'UPPER':
                stat_defs = [("Max", lambda s: s.max()), ("P90", lambda s: s.quantile(0.90)),
                             ("Med", lambda s: s.median()), ("P10", lambda s: s.quantile(0.10))]
            elif direction == 'LOWER':
                stat_defs = [("P90", lambda s: s.quantile(0.90)), ("Med", lambda s: s.median()),
                             ("P10", lambda s: s.quantile(0.10)), ("Min", lambda s: s.min())]
            else:
                stat_defs = [("P90", lambda s: s.quantile(0.90)), ("Med", lambda s: s.median()),
                             ("P10", lambda s: s.quantile(0.10)),
                             ("Std", lambda s: s.std() if len(s) > 1 else float('nan'))]
            row_labels = [lbl for lbl, _ in stat_defs]
            for r_idx, label in enumerate(row_labels):
                table_shape.cell(r_idx + 1, 0).text = label

            # Wafer별 통계 (1~25 고정 루프, Stat 라벨이 0열이므로 wafer #w → c_idx=w)
            for w_idx in range(1, 26):
                w_str = str(w_idx)
                c_idx = w_idx
                if w_str in grouped.groups:
                    grp_data = pd.to_numeric(grouped.get_group(w_str), errors='coerce').dropna()
                    for r_idx, (_lbl, _fn) in enumerate(stat_defs, start=1):
                        try:
                            _v = _fn(grp_data)
                            table_shape.cell(r_idx, c_idx).text = "-" if pd.isna(_v) else f"{_v:.3f}"
                        except Exception:
                            table_shape.cell(r_idx, c_idx).text = "-"
                else:
                    for r_idx in range(1, 5):
                        table_shape.cell(r_idx, c_idx).text = "-"
                
            for r_idx, row in enumerate(table_shape.rows):
                if r_idx == 0: continue
                bg_color = RGBColor(245, 247, 250) if r_idx % 2 == 1 else RGBColor(255, 255, 255)
                for c_idx, cell in enumerate(row.cells):
                    w_num_str = str(c_idx)  # 0열=Stat 라벨, 1~25열=wafer #1~#25
                    if c_idx >= 1 and w_num_str not in grouped.groups:
                        # 빈 데이터 웨이퍼는 연한 그레이 채우기
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = RGBColor(240, 240, 240)
                    else:
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = bg_color
                    cell.margin_left = Inches(0.0)
                    cell.margin_right = Inches(0.0)
                    cell.margin_top = Inches(0.0)
                    cell.margin_bottom = Inches(0.0)
                    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                    cell.text_frame.word_wrap = False
                    for par in cell.text_frame.paragraphs:
                        par.font.size = Pt(7)
                        par.font.color.rgb = RGBColor(0, 0, 0)
                        par.font.name = FONT
                        par.alignment = PP_ALIGN.CENTER

            # ---- 차트 임시 버퍼 (디스크에 파일 생성하지 않고 메모리에서 처리 → PPT 임베드 후 폐기) ----
            import io as _io
            tmp_box = _io.BytesIO()
            tmp_map = _io.BytesIO()
            tmp_trend = _io.BytesIO()
            tmp_rad = _io.BytesIO()
            tmp_cum = _io.BytesIO()
            tmp_leg = _io.BytesIO()
            
            plt.rcParams['axes.linewidth'] = 0.6
            plt.rcParams['font.size'] = 7.5
            plt.rcParams['font.family'] = FONT
            plt.rcParams['axes.facecolor'] = '#ffffff'
            plt.rcParams['figure.facecolor'] = '#ffffff'
            plt.rcParams['grid.color'] = C_GRID
            plt.rcParams['grid.linestyle'] = '-'
            plt.rcParams['grid.linewidth'] = 0.5

            # 차트 축 라벨 공통 스타일 헬퍼 (단위 컬럼이 없어 항목명/의미 라벨 사용)
            def _label_axes(ax, xlabel=None, ylabel=None):
                if xlabel is not None:
                    ax.set_xlabel(xlabel, fontsize=7, color=C_NAVY, fontname=FONT)
                if ylabel is not None:
                    ax.set_ylabel(ylabel, fontsize=7, color=C_NAVY, fontname=FONT)

            # Wafer 색상 고정 매핑은 루프 밖에서 WAFER_PALETTE로 1회 정의됨(w_colors)

            # ---- Wafer Color Legend ----
            #  (lot_id↔wafer 구분은 Radius/Cumulative의 Lot marker 범례 및 Trend 색으로 표기)
            fig_leg, ax_leg = plt.subplots(figsize=(8.0, 0.2))
            plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
            ax_leg.axis('off')
            ax_leg.text(0, 0, "Wafer Color:", va='center', ha='left', fontsize=8, fontweight='bold', color=C_NAVY)
            for i in range(1, 26):
                ax_leg.scatter([i*0.85 + 2.5], [0], color=w_colors.get(str(i)), s=25)
                ax_leg.text(i*0.85 + 2.5 + 0.15, 0, f"#{i}", va='center', ha='left', fontsize=6, color=C_NEUTRAL)
            ax_leg.set_xlim(0, 25)
            ax_leg.set_ylim(-1, 1)
            fig_leg.savefig(tmp_leg, format='jpg', dpi=dpi, facecolor='#f8fafc', pil_kwargs={'quality': jpg_q})
            plt.close(fig_leg)
            tmp_leg.seek(0)
            slide.shapes.add_picture(tmp_leg, Inches(LX), Inches(Y_LEG), width=Inches(LW))

            # ---- 3. BOX Plot (측정된 웨이퍼만 플롯하되, X축은 1~25 고정) ----
            fig_box, ax_box = plt.subplots(figsize=(7.8, 1.9))
            box_data = []
            box_positions = []
            for w in measured_wafers:
                try:
                    w_int = int(w)
                    box_positions.append(w_int)
                    box_data.append(grouped.get_group(w).values)
                except ValueError: pass
                    
            if len(box_data) > 0:
                bplot = ax_box.boxplot(
                    box_data, positions=box_positions, patch_artist=True,
                    showfliers=True,
                    flierprops={'marker': 'o', 'markerfacecolor': C_ACCENT, 'markeredgecolor': 'none', 'markersize': 3.5},
                    medianprops={'color': C_NEUTRAL, 'linewidth': 1.2},
                    whiskerprops={'color': '#94a3b8', 'linewidth': 0.8},
                    capprops={'color': '#94a3b8', 'linewidth': 0.8}
                )
                for i, patch in enumerate(bplot['boxes']):
                    w_str = str(box_positions[i])
                    patch.set_facecolor(w_colors.get(w_str, '#ffffff'))
                    patch.set_alpha(0.85)
                    patch.set_edgecolor(C_NEUTRAL)
                    patch.set_linewidth(0.8)

            # Spec line은 그리되 범례(Spec Limit)는 표시하지 않음
            if spec_low is not None:
                ax_box.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            if spec_high is not None:
                ax_box.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            ax_box.set_xticks(range(1, 26))
            ax_box.set_xticklabels([f"#{i}" for i in range(1, 26)])
            ax_box.set_xlim(0.5, 25.5)
            ax_box.tick_params(axis='x', rotation=45, labelsize=7)
            _label_axes(ax_box, xlabel="Wafer #", ylabel=y_label)
            if log_scale: ax_box.set_yscale('log')
            _remove_spines(ax_box)
            ax_box.set_axisbelow(True)
            ax_box.minorticks_off()  # minor tick(세부선) 제거 — major만 표시
            ax_box.grid(True, which='major', axis='both', color=C_GRID, linestyle='-', linewidth=0.5)
            # 용량 다이어트를 위한 JPG 포맷 저장 및 quality 옵션 적용
            fig_box.savefig(tmp_box, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_box)
            tmp_box.seek(0)
            slide.shapes.add_picture(tmp_box, Inches(LX), Inches(Y_BOX), Inches(LW), Inches(2.05))

            # ---- 4. WF MAP (PGM/Wafer 다중 분할, 모자이크 타일 방식 적용, 3행 기준 고정 비율) ----
            # Wafer 좌표: flat-zone 회전이 반영된 보정 좌표(CHIP_X_ADJ/CHIP_Y_ADJ)가
            # 있으면 우선 사용해 실제 웨이퍼 배치대로 그린다. 없으면 raw chip_x/y로 fallback.
            map_x = 'CHIP_X_ADJ' if 'CHIP_X_ADJ' in item_df.columns else col_x
            map_y = 'CHIP_Y_ADJ' if 'CHIP_Y_ADJ' in item_df.columns else col_y

            sub_groups = list(item_df.groupby(col_sub))
            n_pgm = len(sub_groups) if len(sub_groups) > 0 else 1
            # 측정된 wafer만 컬럼으로 표시(빈 컬럼 제거 → 각 wafer 셀을 최대한 크게)
            measured_sorted = sorted(measured_wafers, key=lambda x: int(x) if str(x).isdigit() else 0)
            n_waf = max(len(measured_sorted), 1)

            global_x_min = item_df[map_x].min()
            global_x_max = item_df[map_x].max()
            global_y_min = item_df[map_y].min()
            global_y_max = item_df[map_y].max()

            x_range = global_x_max - global_x_min if global_x_max > global_x_min else 1
            y_range = global_y_max - global_y_min if global_y_max > global_y_min else 1
            # 가장자리 칩(사각 마커)이 잘리지 않도록 반칩 이상 여백 확보 (칩 간격 기준)
            _nxu = max(int(item_df[map_x].nunique()), 1)
            _nyu = max(int(item_df[map_y].nunique()), 1)
            x_pad = (x_range / max(_nxu - 1, 1)) * 0.7 if _nxu > 1 else x_range * 0.1
            y_pad = (y_range / max(_nyu - 1, 1)) * 0.7 if _nyu > 1 else y_range * 0.1
            global_x_min -= x_pad
            global_x_max += x_pad
            global_y_min -= y_pad
            global_y_max += y_pad

            # ---- WF MAP 공유 컬러 스케일 (spec line 기준 diverging) ----
            # 모든 PGM(pt) 행이 '같은' 스케일/컬러바 1개를 공유한다.
            # speclow→파랑끝 · spechigh→빨강끝 · median→연회색 center (LOWER는 cmap 반전으로 색 반대).
            # HTML Score Board WF MAP과 동일 규칙(_wfmap_norm)으로 산출 → PPT/HTML 색이 일치.
            wfmap_norm = _wfmap_norm(direction, wfmap_spec_low, wfmap_spec_high, item_df[item_name])

            # 칩 격자 차원(예: 13x13)
            nx = max(int(item_df[map_x].nunique()), 1)
            ny = max(int(item_df[map_y].nunique()), 1)
            gdim = max(nx, ny)
            map_avail = SLIDE_BOTTOM - Y_MAP

            # ---- WF MAP 배치: 행=PGM(pt), 열=wafer #1~25 고정 ----
            # 측정 wafer 수와 무관하게 항상 25칸(고정) → WF MAP 전체 크기가 1매든 25매든 동일.
            # 없는 wafer 자리는 빈 칸으로 둔다. die별 색은 spec/median 기준(_wfmap_norm/_wfmap_cmap, HTML과 동일).
            FIXED_N_WAF = 25
            grid_rows, grid_cols = n_pgm, FIXED_N_WAF
            _multi_pgm = n_pgm > 1
            # (pgm_row, wafer_col) → (sub_grp, wafer_str, sub_name)  — 열 c는 항상 wafer #(c+1)
            cell_map = {(i, c): (sub_grp, str(c + 1), sub_name)
                        for i, (sub_name, sub_grp) in enumerate(sub_groups)
                        for c in range(FIXED_N_WAF)}

            _cbar_w = 0.45                                 # 우측 컬러바 폭(인치)
            render_cell = 0.55                             # 셀(=wafer 1칸) 크기(인치) — 25칸 고정
            chip_pt = render_cell / gdim * 72.0            # 칩 1개 한 변(pt)
            marker_s = max(0.5, (chip_pt * 0.9) ** 2)      # 격자가 커도(13x13) 겹치지 않게 자동 축소
            fig_disp_w = grid_cols * render_cell           # 서브플롯 영역 폭(컬러바 제외, 항상 25*cell)
            fig_h = grid_rows * render_cell + 0.15         # 하단 wafer 번호 라벨 여백

            fig_map, axes_map = plt.subplots(grid_rows, grid_cols,
                                             figsize=(fig_disp_w + _cbar_w, fig_h), squeeze=False,
                                             gridspec_kw={'wspace': 0.06, 'hspace': 0.18})
            sc = None
            for r in range(grid_rows):
                for c in range(grid_cols):
                    ax = axes_map[r, c]
                    sub_grp, w, sub_name = cell_map[(r, c)]
                    w_grp = sub_grp[sub_grp[w_col] == w]
                    if not w_grp.empty:   # 측정된 wafer만 die 산점, 없으면 빈 칸
                        sc = ax.scatter(w_grp[map_x], w_grp[map_y], c=w_grp[item_name],
                                        cmap=_wfmap_cmap(direction), norm=wfmap_norm,
                                        s=marker_s, marker='s', alpha=1.0, linewidths=0)
                    ax.set_facecolor('white')
                    # 마지막(맨 아래) PGM 행에만 wafer 번호 표기
                    if r == grid_rows - 1:
                        ax.set_xlabel(f"#{c + 1}", fontsize=5, labelpad=1, color=C_NEUTRAL)
                    # 다중 PGM이면 첫 열 좌측에 PGM(pt) 라벨(90도 회전)
                    if c == 0 and _multi_pgm:
                        _pl = str(sub_name)
                        if 'PGM(pt)' in sub_grp.columns:
                            _u = sub_grp['PGM(pt)'].dropna()
                            if len(_u): _pl = str(_u.iloc[0])
                        ax.set_ylabel(_pl, fontsize=6, rotation=90, labelpad=6, color=C_NEUTRAL)
                    ax.set_xticks([]); ax.set_yticks([])
                    ax.set_aspect('equal', adjustable='box')   # 웨이퍼 정원형 유지
                    for spine in ax.spines.values():
                        spine.set_visible(False)
                    ax.set_xlim(global_x_min, global_x_max)
                    ax.set_ylim(global_y_min, global_y_max)

            if sc is not None:
                # 모든 wafer가 공유하는 단일 컬러바를 오른쪽에 배치
                _cb_frac = fig_disp_w / (fig_disp_w + _cbar_w)
                fig_map.subplots_adjust(left=0.02, right=_cb_frac, top=0.97, bottom=0.06)
                cbar_ax = fig_map.add_axes([_cb_frac + 0.015, 0.12, 0.012, 0.76])
                cbar = fig_map.colorbar(sc, cax=cbar_ax)
                cbar.ax.tick_params(labelsize=6)
            # 칩 격자가 선명하도록 해상도 상향
            fig_map.savefig(tmp_map, format='jpg', dpi=max(int(dpi), 200), bbox_inches="tight",
                            facecolor='white', pil_kwargs={'quality': map_q})
            plt.close(fig_map)

            # 고정 크기 배치: 25칸 고정이므로 aspect가 항상 동일 → wafer 수와 무관하게 같은 크기.
            # 좌열 폭(LW)에 맞추되 세로가 map_avail을 넘으면만 축소.
            _ratio = fig_h / (fig_disp_w + _cbar_w)
            pic_w, pic_h = LW, LW * _ratio
            if pic_h > map_avail:
                pic_h = map_avail
                pic_w = pic_h / _ratio
            tmp_map.seek(0)
            slide.shapes.add_picture(tmp_map, Inches(LX), Inches(Y_MAP), Inches(pic_w), Inches(pic_h))

            # ---- 5. Trend Chart (우측 상단 - vehicle/with_vehicle/target 비교 + vehicle 1~99% 구름대) ----
            import matplotlib.dates as mdates
            tdf = item_df_full.copy()
            tdf['tkout_time'] = pd.to_datetime(tdf['tkout_time'])
            has_mask = 'mask' in tdf.columns
            has_lot = 'fab_lot_id' in tdf.columns

            # ---- 특정 항목: site별 모든 값 대신 tkout_time 기준 집계점으로 Trend 표시 ----
            #   config.trend_tkout_agg = {ALIAS: 'P10'/'P90'/'MEDIAN'/'MEAN'}.
            #   (lot/mask/wafer + tkout_time)별로 site 값을 1점으로 집계 → 같은 측정의 site 노이즈 제거.
            _agg_map = getattr(GLOBAL_CONFIG, 'trend_tkout_agg', {}) or {}
            _agg_spec = _agg_map.get(item_name) or _agg_map.get(spec_name)
            if _agg_spec:
                _s = str(_agg_spec).strip().upper()
                if _s in ('MEAN', 'AVG'):
                    _aggf = 'mean'
                elif _s in ('MEDIAN', 'P50'):
                    _aggf = 'median'
                else:
                    _m = re.match(r'P(\d+(?:\.\d+)?)$', _s)
                    _aggf = (lambda s, _q=min(max(float(_m.group(1)) / 100.0, 0.0), 1.0): s.quantile(_q)) if _m else 'median'
                _gk = [k for k in ['mask', 'fab_lot_id', 'root_lot_id', 'wafer_id', 'match_key', 'tkout_time']
                       if k in tdf.columns]
                if 'tkout_time' in _gk and len(_gk) > 1:
                    tdf = (tdf.dropna(subset=[item_name])
                              .groupby(_gk, as_index=False).agg({item_name: _aggf}))
                    tdf['tkout_time'] = pd.to_datetime(tdf['tkout_time'])

            veh_df = tdf[tdf['mask'] == main_vehicle] if has_mask else tdf

            def _draw_trend(ax):
                # vehicle 기준 1~99% 구름대 + median (main vehicle 데이터만)
                if len(veh_df) > 0:
                    b = veh_df[['tkout_time', item_name]].dropna().sort_values('tkout_time')
                    if len(b) > 0:
                        b = b.assign(date=b['tkout_time'].dt.date)
                        daily = b.groupby('date')[item_name].agg(
                            median='median',
                            q01=lambda x: x.quantile(0.01),
                            q99=lambda x: x.quantile(0.99)).reset_index()
                        daily['date'] = pd.to_datetime(daily['date'])
                        daily = daily.set_index('date').sort_index()
                        roll = daily.rolling('3D', min_periods=1).mean()
                        ax.fill_between(roll.index, roll['q01'], roll['q99'], color=C_BAND, alpha=0.6, label=f'{main_vehicle} 1~99%', zorder=1)
                        # 3일 기준 rolling median 라인 (검정색)
                        ax.plot(roll.index, roll['median'], color='black', linewidth=1.5, alpha=1.0, zorder=6, label='3-day median')
                # 색상 규칙: target lot=빨강(최상단) / 같은 vehicle 나머지=초록 / with_vehicle=회색
                # 모든 마커는 얇은 검정 테두리(edgecolors='black', linewidths 얇게)를 적용
                if has_lot:
                    tgt = _select_target_lot(tdf)                 # 대상 lot (robust 매칭)
                    tgt_idx = set(tgt.index)
                    if has_mask:
                        # 같은 vehicle이면서 대상 lot이 아닌 데이터 → 초록
                        veh_other = tdf[(tdf['mask'] == main_vehicle) & (~tdf.index.isin(tgt_idx))]
                        # with_vehicle(다른 vehicle) 데이터
                        wv = tdf[tdf['mask'] != main_vehicle]
                    else:
                        veh_other = tdf[~tdf.index.isin(tgt_idx)]; wv = tdf.iloc[0:0]
                    if len(veh_other) > 0:
                        # 범례에 실제 vehicle 명(config.yaml) 표기
                        ax.scatter(veh_other['tkout_time'], veh_other[item_name], s=10, alpha=0.5, color=C_VEHICLE, label=str(main_vehicle), edgecolors='black', linewidths=0.3, zorder=2)
                    if len(wv) > 0:
                        # with_vehicle은 mask(=실제 vehicle 명)별로 분리하여 각각 다른 색 + 개별 범례
                        for _wi, _wv_name in enumerate(sorted(wv['mask'].dropna().unique()) if has_mask else []):
                            _wv_grp = wv[wv['mask'] == _wv_name]
                            if len(_wv_grp) == 0: continue
                            _wv_color = WV_PALETTE[_wi % len(WV_PALETTE)]
                            ax.scatter(_wv_grp['tkout_time'], _wv_grp[item_name], s=10, alpha=0.5, color=_wv_color, label=str(_wv_name), edgecolors='black', linewidths=0.3, zorder=3)
                    if len(tgt) > 0:
                        if multi_lot and lot_col:
                            # 같은 root의 여러 lot_id → lot별 색으로 구분 (범례=lot_id)
                            for _lot in target_lots:
                                _tl = tgt[tgt[lot_col].astype(str) == _lot]
                                if len(_tl) == 0: continue
                                ax.scatter(_tl['tkout_time'], _tl[item_name], s=26, alpha=1.0,
                                           color=lot_color[_lot], marker=lot_marker.get(_lot, 'o'),
                                           label=f"{_lot}_{target_DC_step_id}", edgecolors='black', linewidths=0.4, zorder=10)
                        else:
                            # 단일 lot: 빨간색 + search_key 라벨
                            _tgt_label = f"{target_lot_id}_{target_DC_step_id}"
                            ax.scatter(tgt['tkout_time'], tgt[item_name], s=24, alpha=1.0, color='red',
                                       label=_tgt_label, edgecolors='black', linewidths=0.4, zorder=10)
                else:
                    for w in measured_wafers:
                        grp = tdf[tdf[w_col] == w] if w_col in tdf.columns else tdf.iloc[0:0]
                        ax.scatter(grp['tkout_time'], grp[item_name], s=10, alpha=0.7, color=w_colors.get(str(w), 'blue'), edgecolors='black', linewidths=0.3, zorder=2)
                # spec line(s) — 방향(REPORT DIRECTION) 반영된 spec_low/high (범례에는 표시하지 않음)
                if spec_low is not None:
                    ax.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
                if spec_high is not None:
                    ax.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
                ax.set_title("")
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
                ax.tick_params(axis='x', rotation=0, labelsize=7)
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=6))
                _label_axes(ax, xlabel="Date", ylabel=y_label)
                if log_scale: ax.set_yscale('log')
                ax.legend(fontsize=6, loc='upper left', frameon=False)   # Trend 범례 좌상단 고정
                _remove_spines(ax)
                ax.minorticks_off()  # minor tick(세부선) 제거 — major만 표시
                ax.grid(True, which='major', color=C_GRID, linestyle='-', linewidth=0.5)

            fig_trend, ax_trend = plt.subplots(figsize=(4.55, 1.75))
            _draw_trend(ax_trend)
            fig_trend.savefig(tmp_trend, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_trend)
            tmp_trend.seek(0)
            slide.shapes.add_picture(tmp_trend, Inches(RX), Inches(Y_TREND), Inches(RW), Inches(1.95))

            # index(alias) Trend scatter 차트만 RUN/TEMP에 alias명.png로 저장 (Anomaly/HTML 재사용)
            try:
                fig_trend_png, ax_trend_png = plt.subplots(figsize=(4.55, 2.0))
                _draw_trend(ax_trend_png)
                _html_dpi = getattr(GLOBAL_CONFIG, 'html_chart_dpi', 100)
                fig_trend_png.savefig(f"RUN/TEMP/{safe_name}.png", dpi=_html_dpi, bbox_inches="tight")
                plt.close(fig_trend_png)
            except Exception as e:
                print(f"[WARN] Failed to save RUN/TEMP/{safe_name}.png: {e}")

            # 지표 (Metrics) 계산 및 저장
            try:
                # global = vehicle 모집단(veh_df), target = 리포팅 대상 lot(item_df=target_df)
                _gsrc = veh_df if len(veh_df) > 0 else item_df
                g_med = float(_gsrc[item_name].median())
                g_std = float(_gsrc[item_name].std())

                t_df = item_df
                t_med = float(t_df[item_name].median()) if len(t_df) > 0 else g_med
                t_std = float(t_df[item_name].std()) if len(t_df) > 1 else g_std

                dev = round(abs(t_med - g_med) / g_std, 2) if g_std > 0 else 0.0

                # spec-out(이상) 분류는 '해당 lot_id'에만 한정 — 같은 root의 형제 lot은 이상으로 분류하지 않음
                if 'fab_lot_id' in t_df.columns:
                    _t_spec = t_df[t_df['fab_lot_id'].astype(str) == str(target_lot_id)]
                    if len(_t_spec) == 0:
                        _t_spec = t_df
                else:
                    _t_spec = t_df

                s_outs = []
                for _, row in _t_spec.iterrows():
                    v = row[item_name]
                    if (spec_low is not None and v < float(spec_low)) or (spec_high is not None and v > float(spec_high)):
                        cx = row.get('CHIP_X_ADJ', row.get(col_x, 0))
                        cy = row.get('CHIP_Y_ADJ', row.get(col_y, 0))
                        s_outs.append({"val": float(v), "x": float(cx), "y": float(cy)})
                
                metrics_dict[item_name] = {
                    "item": item_name,
                    "global_med": g_med,
                    "global_std": g_std,
                    "target_med": t_med,
                    "target_std": t_std,
                    "deviation": dev,
                    "spec_outs": s_outs,
                    "spec_out_count": len(s_outs)
                }
            except Exception as e:
                print(f"[WARN] Failed to calculate metrics for {item_name}: {e}")

            # ---- 6. Radius Plot (반경별 분포 + 3차 다항식 근사) ----
            fig_rad, ax_rad = plt.subplots(figsize=(4.55, 1.75))
            for w in measured_wafers:
                w_df = item_df[item_df[w_col] == w].dropna(subset=[col_rad, item_name]).sort_values(by=col_rad)
                if len(w_df) == 0: continue
                p_color = w_colors.get(str(w), 'blue')
                # 색=wafer, marker=lot_id (multi_lot일 때만 lot별 모양 구분)
                if multi_lot and lot_col:
                    for _lot in target_lots:
                        _wl = w_df[w_df[lot_col].astype(str) == _lot]
                        if len(_wl) == 0: continue
                        ax_rad.scatter(_wl[col_rad], _wl[item_name], s=14, alpha=0.85, color=p_color,
                                       marker=lot_marker[_lot], edgecolors='black', linewidths=0.2)
                else:
                    ax_rad.scatter(w_df[col_rad], w_df[item_name], s=12, alpha=0.85, color=p_color)
                if len(w_df) >= 4:
                    try:
                        z = np.polyfit(w_df[col_rad], w_df[item_name], 3)
                        p = np.poly1d(z)
                        ax_rad.plot(np.linspace(w_df[col_rad].min(), w_df[col_rad].max(), 50),
                                   p(np.linspace(w_df[col_rad].min(), w_df[col_rad].max(), 50)),
                                   color=p_color, alpha=0.6, linewidth=1.5)
                    except: pass
            # lot_id marker 범례 (multi_lot)
            if multi_lot:
                from matplotlib.lines import Line2D
                _lh = [Line2D([0], [0], marker=lot_marker[_lot], color='#444444', linestyle='none',
                              markersize=6, label=str(_lot)) for _lot in target_lots]
                ax_rad.legend(handles=_lh, fontsize=6, loc='upper left', bbox_to_anchor=(1.0, 1.0),
                              frameon=False, title='Lot', title_fontsize=6)   # 차트 밖 우상단 고정
            # Spec line은 그리되 범례(Spec Limit)는 표시하지 않음
            if spec_low is not None:
                ax_rad.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            if spec_high is not None:
                ax_rad.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            ax_rad.set_title("")
            _label_axes(ax_rad, xlabel="Chip Radius", ylabel=y_label)
            if log_scale: ax_rad.set_yscale('log')
            _remove_spines(ax_rad)
            ax_rad.minorticks_off()  # minor tick(세부선) 제거 — major만 표시
            ax_rad.grid(True, which='major', color=C_GRID, linestyle='-', linewidth=0.5)
            fig_rad.savefig(tmp_rad, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_rad)
            tmp_rad.seek(0)
            slide.shapes.add_picture(tmp_rad, Inches(RX), Inches(Y_RAD), Inches(RW), Inches(1.95))

            # ---- 7. Cumulative Plot (누적 분포) ----
            fig_cum, ax_cum = plt.subplots(figsize=(4.55, 1.85))
            for w in fixed_wafers:
                p_color = w_colors.get(str(w), 'blue')
                if multi_lot and lot_col:
                    # 색=wafer, marker=lot_id. 같은 wafer라도 lot별로 모양이 달라짐
                    for _lot in target_lots:
                        _sub = item_df[(item_df[w_col] == w) & (item_df[lot_col].astype(str) == _lot)][item_name].dropna()
                        if len(_sub) == 0: continue
                        _sd = np.sort(_sub.values)
                        _yv = np.arange(len(_sd)) / float(len(_sd) - 1) if len(_sd) > 1 else [1.0]
                        ax_cum.plot(_sd, _yv, marker=lot_marker[_lot], linestyle='-', markersize=4,
                                    linewidth=0.9, color=p_color, alpha=0.8)
                elif w in grouped.groups:
                    grp = grouped.get_group(w)
                    sorted_data = np.sort(grp.values)
                    yvals = np.arange(len(sorted_data)) / float(len(sorted_data) - 1) if len(sorted_data) > 1 else [1.0]
                    # 같은 wafer_id 데이터를 선으로 연결 (마커 + 라인)
                    ax_cum.plot(sorted_data, yvals, marker='.', linestyle='-', markersize=4, linewidth=1.0, color=p_color, alpha=0.8)
            if multi_lot:
                from matplotlib.lines import Line2D
                _lh = [Line2D([0], [0], marker=lot_marker[_lot], color='#444444', linestyle='none',
                              markersize=6, label=str(_lot)) for _lot in target_lots]
                ax_cum.legend(handles=_lh, fontsize=6, loc='upper left', bbox_to_anchor=(1.0, 1.0),
                              frameon=False, title='Lot', title_fontsize=6)   # 차트 밖 우상단 고정
            # Spec line은 그리되 범례(Spec Limit)는 표시하지 않음
            if spec_low is not None:
                ax_cum.axvline(x=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            if spec_high is not None:
                ax_cum.axvline(x=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7)
            ax_cum.set_title("")
            _label_axes(ax_cum, xlabel=y_label, ylabel="Cumulative Prob.")
            if log_scale: ax_cum.set_xscale('log')
            _remove_spines(ax_cum)
            ax_cum.minorticks_off()  # minor tick(세부선) 제거 — major만 표시
            ax_cum.grid(True, which='major', color=C_GRID, linestyle='-', linewidth=0.5)
            fig_cum.savefig(tmp_cum, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_cum)
            tmp_cum.seek(0)
            slide.shapes.add_picture(tmp_cum, Inches(RX), Inches(Y_CUM), Inches(RW), Inches(2.15))

            # ---- 임시 차트 버퍼 해제 (디스크 파일 없음) ----
            for _b in [tmp_box, tmp_map, tmp_trend, tmp_rad, tmp_cum, tmp_leg]:
                try: _b.close()
                except Exception: pass
            gc.collect()

        except Exception as e:
            import traceback
            print(f"[ERROR] {item_name} 차트 생성 중 에러 발생: {e}")
            traceback.print_exc()

    print("=" * 60)
    print(f"[insert_plots] 차트 생성 완료 - 총 {total_items}개 index 처리")
    print("=" * 60)

    # ==================== 마지막 Index Aggregation Table 페이지(들) ====================
    # 값 산출 기준: REPORT DIRECTION → BOTH=Median / UPPER=P90 / LOWER=P10
    # index가 많으면 30개씩 페이지 분할. 모든 페이지가 동일한 (lot,wafer) 컬럼 구성을
    # 공유하여 칸 크기가 페이지마다 바뀌지 않도록 한다.
    if summary_rows:
        try:
            def _set_cell_borders(cell):
                from pptx.oxml.xmlchemy import OxmlElement
                tcPr = cell._tc.get_or_add_tcPr()
                for t in ['lnL', 'lnR', 'lnT', 'lnB']:
                    for el in tcPr.findall(f'{{http://schemas.openxmlformats.org/drawingml/2006/main}}{t}'):
                        tcPr.remove(el)
                _bi = 0
                for t in ['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB']:
                    ln = OxmlElement(t); ln.set('w', '12700'); ln.set('cmpd', 'sng')
                    sf = OxmlElement('a:solidFill'); sc = OxmlElement('a:srgbClr'); sc.set('val', '333333')
                    sf.append(sc); ln.append(sf); tcPr.insert(_bi, ln); _bi += 1

            def _fmt_num(v):
                try:
                    f = float(v)
                except (ValueError, TypeError):
                    return str(v)
                if f != 0 and (abs(f) > 10000 or abs(f) <= 0.0001):
                    mant, exp = f"{f:.1E}".split('E')
                    return f"{mant}E{int(exp)}"
                if abs(f) >= 100:          # 100 이상은 소수점 첫째자리까지 (예: 1234.5)
                    return f"{f:.1f}"
                return f"{f:.3f}"          # 100 미만은 기존대로(소수점 3자리)

            def _style(cell, text, bg, fg, bold=False, sz=8, wrap=False):
                cell.text = str(text)
                cell.fill.solid(); cell.fill.fore_color.rgb = bg
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.text_frame.word_wrap = wrap
                _set_cell_borders(cell)
                cell.margin_left = Inches(0.01); cell.margin_right = Inches(0.01)
                cell.margin_top = Inches(0.0); cell.margin_bottom = Inches(0.0)
                for par in cell.text_frame.paragraphs:
                    par.font.size = Pt(sz); par.font.bold = bold; par.font.name = FONT
                    par.font.color.rgb = fg; par.alignment = PP_ALIGN.CENTER

            # ---- (lot, wafer) 컬럼 구성(전체 index 기준, 모든 페이지 공통) ----
            _all_keys = set()
            for _r in summary_rows:
                _all_keys.update(_r.get('wafer_vals', {}).keys())
            _lots = sorted({k[0] for k in _all_keys},
                           key=lambda l: (0 if str(l) == str(target_lot_id) else 1, str(l)))
            def _wkey(k):
                return k[1] if isinstance(k[1], int) else 10 ** 9
            _lot_groups = []   # (lot, [(lot,wafer), ...])
            _ordered = []
            for _lot in _lots:
                _wc = sorted([k for k in _all_keys if k[0] == _lot], key=_wkey)
                if _wc:
                    _lot_groups.append((_lot, _wc))
                    _ordered.extend(_wc)

            NAVY = RGBColor(*NAVY_RGB)
            LOTBG = RGBColor(0xD9, 0xE1, 0xF2)
            WAFBG = RGBColor(0xF0, 0xF0, 0xF0)
            white = RGBColor(255, 255, 255); black = RGBColor(0, 0, 0)

            # 고정 열 너비: Index 넓게(잘림 방지) + wafer 좁게(상한 캡으로 일정 유지)
            _idx_w, _stat_w = 2.05, 0.55
            _ww = min(0.40, max(0.14, (13.333 - 0.24 - _idx_w - _stat_w) / max(len(_ordered), 1)))
            _tbl_w = min(13.10, _idx_w + _stat_w + _ww * len(_ordered))

            _CHUNK = 30
            _total_pages = (len(summary_rows) - 1) // _CHUNK + 1
            for _pg in range(_total_pages):
                _chunk = summary_rows[_pg * _CHUNK:(_pg + 1) * _CHUNK]
                s_slide = prs.slides.add_slide(prs.slide_layouts[6])
                s_slide.background.fill.solid()
                s_slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)

                # 상단 헤더 바
                hdr = s_slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.62))
                hdr.fill.solid(); hdr.fill.fore_color.rgb = NAVY; hdr.line.fill.background()
                htf = hdr.text_frame; htf.word_wrap = True; htf.margin_left = Inches(0.2)
                hp = htf.paragraphs[0]
                _sfx = f"  ({_pg + 1}/{_total_pages})" if _total_pages > 1 else ""
                hp.text = f"Index Aggregation Table{_sfx}"
                hp.font.size = Pt(18); hp.font.bold = True
                hp.font.color.rgb = RGBColor(255, 255, 255); hp.font.name = FONT
                hp.alignment = PP_ALIGN.LEFT

                ncol = 2 + len(_ordered)
                nrow = 2 + len(_chunk)
                tbl_h = Inches(min(6.5, max(0.26 * nrow, 0.6)))
                table = s_slide.shapes.add_table(nrow, ncol, Inches(0.12), Inches(0.78),
                                                 Inches(_tbl_w), tbl_h).table
                table.columns[0].width = Inches(_idx_w)
                table.columns[1].width = Inches(_stat_w)
                for _ci in range(2, ncol):
                    table.columns[_ci].width = Inches(_ww)

                # 헤더: Index/Stat 세로 병합, lot 가로 병합 + 그 아래 #wafer
                table.cell(0, 0).merge(table.cell(1, 0)); _style(table.cell(0, 0), "Index", NAVY, white, True, 9, wrap=True)
                table.cell(0, 1).merge(table.cell(1, 1)); _style(table.cell(0, 1), "Stat", NAVY, white, True, 8)
                _ci = 2
                for _lot, _wc in _lot_groups:
                    _c0 = _ci; _c1 = _ci + len(_wc) - 1
                    if _c1 > _c0:
                        table.cell(0, _c0).merge(table.cell(0, _c1))
                    _style(table.cell(0, _c0), str(_lot), LOTBG, black, True, 8)
                    for (_lot2, _waf) in _wc:
                        _style(table.cell(1, _ci), f"#{_waf}", WAFBG, black, True, 7)
                        _ci += 1

                # 본문: Index | Stat | (lot,wafer)별 값
                for _ri, _r in enumerate(_chunk, start=2):
                    _style(table.cell(_ri, 0), str(_r['index']), white, black, True, 8, wrap=True)  # Index명 잘림 방지
                    _style(table.cell(_ri, 1), str(_r.get('stat', '')), white, black, False, 8)
                    _wv = _r.get('wafer_vals', {})
                    for _cj, _key in enumerate(_ordered, start=2):
                        _val = _wv.get(_key)
                        _style(table.cell(_ri, _cj), '' if _val is None else _fmt_num(_val), white, black, False, 7)

            print(f"[insert_plots] Index Aggregation Table 생성 완료 ({len(summary_rows)}개 index, {_total_pages}페이지)")
        except Exception as _e:
            print(f"[WARN] Index Aggregation Table 생성 실패: {_e}")

    return prs, metrics_dict

def etdata_query():
    """ET(Electrical Test) 측정 데이터를 빅데이터 서버에서 쿼리하여 일별 Hive-파티셔닝 parquet로 저장.

    GLOBAL_CONFIG에서 읽어오는 주요 설정:
        - vehicle: 대상 차종(마스크) 이름
        - DB_et_daily: 일별 파켓 저장 루트 경로
        - QueryTimeSpan: 전체 쿼리 기간 (일)
        - SplitTimeSpan: 쿼리 분할 단위 (일)
        - process_id, line_id: 공정/라인 필터
        - et_custom_columns, user_name: 쿼리 파라미터
        - et_log_path: Lot 로그 CSV 경로

    저장 구조 (Hive Partitioning):
        ``{DB_et_daily}/date={YYYY-MM-DD}/data.parquet``
    """
    try : 

        item_et = pd.read_csv(f'reformatter/{GLOBAL_CONFIG.get("vehicle")}_reformatter.csv') 

        # 경로 생성 (DB_et_daily만 — LOTWF 디렉토리는 더 이상 사용하지 않음)
        if not os.path.exists(GLOBAL_CONFIG.get("DB_et_daily")):
            os.makedirs(GLOBAL_CONFIG.get("DB_et_daily"))

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

            # Hive 파티셔닝 구조로 일별 parquet 저장
            # 저장 경로: {DB_et_daily}/date={YYYY-MM-DD}/data.parquet
            DB_et_daily = GLOBAL_CONFIG.get("DB_et_daily")
            for date_val, group in daily_groups:
                partition_dir = os.path.join(DB_et_daily, f'date={date_val}')
                os.makedirs(partition_dir, exist_ok=True)
                group.to_parquet(os.path.join(partition_dir, 'data.parquet'), index=False)

            end_time = time.time()
            elapsed_time = end_time - start_time

            print(f"[ET Query Complete] {GLOBAL_CONFIG.get('vehicle')} ET Query 완료 (소요시간: {elapsed_time:.2f}초)")
            print("="*60 + "\n") 

    except Exception as e:
        print(f"[ERROR] etdata_query 실패: {e}")
        traceback.print_exc()

# 사용중 Inline Query
def inlinedata_query(root_lot_id):
    """Inline(FAB 공정 내) 측정 데이터를 쿼리하여 CSV로 저장 후 DataFrame 반환.

    Parameters
    ----------
    root_lot_id : str
        대상 루트 Lot ID.

    Returns
    -------
    pd.DataFrame
        Inline 측정 데이터. 에러 시 빈 DataFrame 반환.
    """
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

        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_high'] *= 1000
        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_limit'] *= 1000
        Query_Table.loc[rows_to_multiply, 'spc_ctrl_spec_low'] *= 1000

        Query_Table.rename(columns={'step_seq': 'STEP_DESC'}, inplace=True)
        Query_Table = pd.merge(Query_Table, Inline1, on='STEP_DESC', how='left')

        Query_Table.to_csv(GLOBAL_CONFIG.get('DB') + f"{GLOBAL_CONFIG.get('vehicle')}_inline_table.csv", encoding='utf-8-sig', index=False)

        return Query_Table
    
    except Exception as e:
        print(f"inlinedata_query 에러가 발생했습니다: {e}")
        traceback.print_exc()
        return pd.DataFrame()

def wipdata_query():
    """WIP(Work In Progress) 현황 데이터를 쿼리하여 CSV로 저장.

    GLOBAL_CONFIG에서 읽어오는 주요 설정:
        - process_id, line_id: 공정/라인 필터
        - wip_custom_columns, user_name: 쿼리 파라미터
        - DB: 저장 디렉토리 경로
        - vehicle: 대상 차종(마스크) 이름
    """
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
        traceback.print_exc()
