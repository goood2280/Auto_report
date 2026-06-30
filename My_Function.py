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

def make_title_page(template_path, vehicle, lot_id, step_merged):
    """PPT 템플릿을 열어 타이틀 페이지를 설정하고 Presentation 객체 반환."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN

    prs = Presentation(template_path)

    # 첫 번째 슬라이드의 텍스트 박스를 업데이트
    slide = prs.slides[0]
    for shape in slide.shapes:
        if shape.has_text_frame:
            shape.text_frame.clear()
            p = shape.text_frame.paragraphs[0]
            p.text = f"{vehicle} HOL Auto Report"
            p.alignment = PP_ALIGN.CENTER
            for run in p.runs:
                run.font.size = Pt(32)
                run.font.bold = True
                run.font.name = GLOBAL_CONFIG.theme_font_family
            p2 = shape.text_frame.add_paragraph()
            p2.text = f"{lot_id} / {step_merged}"
            p2.alignment = PP_ALIGN.CENTER
            for run in p2.runs:
                run.font.size = Pt(18)
                run.font.name = GLOBAL_CONFIG.theme_font_family
            break

    return prs


def insert_score_board(VIP_group, prs, lot_id, title, spec_data=None, config=None):
    """VIP_group(Pass Rate 표)을 PPT 슬라이드로 30개씩 분할하여 삽입 (항상 #1 ~ #25 고정)."""
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE

    # 색/폰트는 GLOBAL_CONFIG에서 단일 관리 (전 슬라이드 톤 통일)
    NAVY = RGBColor(*GLOBAL_CONFIG.theme_title_color)
    FONT = GLOBAL_CONFIG.theme_font_family

    chunk_size = 30
    total_pages = (len(VIP_group) - 1) // chunk_size + 1

    # 현재 전달된 DataFrame의 컬럼명 중 1~25 숫자로 매핑되는 것만 미리 추출
    valid_cols = {}
    for col in VIP_group.columns:
        col_str = str(col).replace('#', '').strip()
        try:
            w_idx = int(col_str)
            if 1 <= w_idx <= 25:
                valid_cols[w_idx] = col
        except ValueError:
            pass

    for page in range(total_pages):
        chunk_df = VIP_group.iloc[page * chunk_size : (page + 1) * chunk_size]
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        
        # 슬라이드 배경색 설정 (흰색)
        slide_bg = slide.background
        slide_bg.fill.solid()
        slide_bg.fill.fore_color.rgb = RGBColor(255, 255, 255)

        # 다크 네이비 헤더 바 추가
        header_shape = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.58)) # MSO_SHAPE.RECTANGLE = 1
        header_shape.fill.solid()
        header_shape.fill.fore_color.rgb = NAVY
        header_shape.line.fill.background()

        # 제목 (페이지 표시 추가)
        txBox = slide.shapes.add_textbox(Inches(0.2), Inches(0.06), Inches(12.7), Inches(0.5))
        p = txBox.text_frame.paragraphs[0]
        page_suffix = f" ({page+1}/{total_pages})" if total_pages > 1 else ""
        p.text = f"Score Board - {title}{page_suffix}"
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = RGBColor(255, 255, 255)
        p.font.name = FONT

        # 행 개수: 해당 페이지의 데이터 개수 + 헤더 1행
        n_rows = len(chunk_df) + 1
        n_cols = 26 
        
        # 테이블의 높이는 데이터 개수에 비례하도록 조절 (최대 5.8인치)
        table_height = Inches(0.4 + len(chunk_df) * 0.18)
        

        tbl_shape = slide.shapes.add_table(n_rows, n_cols, Inches(0.22), Inches(0.8), Inches(12.89), table_height)
        tbl = tbl_shape.table

        # 열 너비 수동 조정 (ITEM 열은 넓게, Wafer 열들은 좁게 균등 분할)
        tbl.columns[0].width = Inches(1.8)
        for j in range(1, 26):
            tbl.columns[j].width = Inches(11.09 / 25)

        # 1. 헤더 렌더링 및 꾸미기
        header_cell = tbl.cell(0, 0)
        header_cell.text = "ITEM"
        header_cell.fill.solid()
        header_cell.fill.fore_color.rgb = NAVY # 네이비 헤더
        header_cell.text_frame.paragraphs[0].font.color.rgb = RGBColor(255, 255, 255)
        header_cell.text_frame.paragraphs[0].font.bold = True
        header_cell.text_frame.paragraphs[0].font.name = FONT
        header_cell.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        
        for j in range(1, 26):
            cell = tbl.cell(0, j)
            cell.text = f"#{j}"
            cell.fill.solid()
            cell.fill.fore_color.rgb = NAVY # 네이비 헤더
            cell.text_frame.paragraphs[0].font.color.rgb = RGBColor(255, 255, 255)
            cell.text_frame.paragraphs[0].font.bold = True
            cell.text_frame.paragraphs[0].font.name = FONT
            cell.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

        # 2. 데이터 렌더링
        for i, (idx, row) in enumerate(chunk_df.iterrows()):
            item_cell = tbl.cell(i + 1, 0)
            item_cell.text = str(idx)
            bg_color = RGBColor(245, 247, 250) if i % 2 == 1 else RGBColor(255, 255, 255)
            item_cell.fill.solid()
            item_cell.fill.fore_color.rgb = bg_color
            item_cell.text_frame.paragraphs[0].font.color.rgb = RGBColor(0, 0, 0)
            item_cell.text_frame.paragraphs[0].font.name = FONT
            
            for j in range(1, 26):
                cell = tbl.cell(i + 1, j)
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor(85, 85, 85) # 측정 안 된 웨이퍼는 진한 그레이
                
                if j in valid_cols:
                    val = row[valid_cols[j]]
                    if pd.notna(val) and str(val).strip() != "":
                        cell.text = f"{val:.1f}"
                        try:
                            v = float(val)
                            if v >= 99:
                                cell.fill.fore_color.rgb = RGBColor(0, 176, 80)
                            elif v >= 95:
                                cell.fill.fore_color.rgb = RGBColor(146, 208, 80)
                            elif v >= 90:
                                cell.fill.fore_color.rgb = RGBColor(255, 255, 0)
                            else:
                                cell.fill.fore_color.rgb = RGBColor(255, 0, 0)
                        except (ValueError, TypeError):
                            pass

        # 3. 폰트 사이즈 일괄 적용, 정렬 및 100.0 줄바꿈(Wrap) 방지 설정
        def set_cell_borders(c):
            from pptx.oxml.xmlchemy import OxmlElement
            tcPr = c._tc.get_or_add_tcPr()
            for t in ['lnL', 'lnR', 'lnT', 'lnB']:
                for el in tcPr.findall(f'{{http://schemas.openxmlformats.org/drawingml/2006/main}}{t}'):
                    tcPr.remove(el)
            idx = 0
            for t in ['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB']:
                ln = OxmlElement(t)
                ln.set('w', '12700')
                ln.set('cmpd', 'sng')
                sf = OxmlElement('a:solidFill')
                sc = OxmlElement('a:srgbClr')
                sc.set('val', '333333')
                sf.append(sc)
                ln.append(sf)
                tcPr.insert(idx, ln)
                idx += 1

        for ri in range(n_rows):
            for ci in range(n_cols):
                cell = tbl.cell(ri, ci)
                cell.margin_left = Inches(0.0)
                cell.margin_right = Inches(0.0)
                cell.margin_top = Inches(0.0)
                cell.margin_bottom = Inches(0.0)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                set_cell_borders(cell)
                cell.text_frame.word_wrap = False # 줄바꿈 방지하여 100.0 짤림 해결
                for para in cell.text_frame.paragraphs:
                    para.font.size = Pt(8) # 글자 크기 최대화
                    para.font.name = FONT
                    if ci > 0: 
                        para.alignment = PP_ALIGN.CENTER

    return prs

def calcaulate_description_image_info_dict(description_ppt_path, img_quality=20):
    """설명 PPT에서 슬라이드별 좌상단 텍스트(Category)를 추출하고 슬라이드를 이미지로 저장하여 매핑 반환."""
    import os
    import pythoncom
    import win32com.client
    
    desc_dict = {}
    if not os.path.exists(description_ppt_path):
        print(f"[WARN] Description PPT를 찾을 수 없습니다: {description_ppt_path}")
        return desc_dict

    try:
        pythoncom.CoInitialize()
        ppt_app = win32com.client.DispatchEx("PowerPoint.Application")
        # 백그라운드 동작을 위해 경고창 무시 및 숨김 처리 시도
        ppt_app.DisplayAlerts = False
        
        abs_path = os.path.abspath(description_ppt_path)
        presentation = ppt_app.Presentations.Open(abs_path, WithWindow=False)
        
        # 설명 슬라이드 PNG는 별도 폴더(temp_desc_images)를 만들지 않고
        # 런타임 임시 영역(RUN/TEMP)에 저장합니다.
        tmp_dir = os.path.join("RUN", "TEMP")
        os.makedirs(tmp_dir, exist_ok=True)
        
        for i, slide in enumerate(presentation.Slides):
            best_text = f"Slide_{i+1}"
            min_dist = float('inf')
            
            # 슬라이드 내의 모든 도형 스캔 후 (0,0) 좌상단에 가장 가까운 텍스트 탐색
            for shape in slide.Shapes:
                if shape.HasTextFrame and shape.TextFrame.HasText:
                    text = shape.TextFrame.TextRange.Text.strip()
                    if text:
                        dist = shape.Top**2 + shape.Left**2
                        if dist < min_dist:
                            min_dist = dist
                            best_text = text.split('\r')[0].split('\n')[0].strip()
            
            # 파워포인트 특수문자 제거
            best_text = best_text.replace('\x0b', '').strip()
            
            # 슬라이드 전체를 PNG 이미지로 Export
            img_path = os.path.join(tmp_dir, f"desc_slide_{i+1}.png")
            slide.Export(img_path, "PNG")
            desc_dict[best_text.lower()] = img_path
            
        presentation.Close()
        ppt_app.Quit()
    except Exception as e:
        print(f"[ERROR] Description PPT 파싱 중 에러 발생: {e}")
    finally:
        pythoncom.CoUninitialize()
        
    return desc_dict


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
    C_WV = GLOBAL_CONFIG.chart_with_vehicle          # with_vehicle scatter (회색)
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

    # --- 항목별 슬라이드 생성 루프 (Per-Item Slide Generation Loop) ---
    for item_name in spec_data.index:
        if item_name not in merged_df.columns:
            continue
            
        # ---- 카테고리 간지(Description) 슬라이드 삽입 ----
        if 'CAT2' in spec_data.columns:
            cat2 = str(spec_data.loc[item_name, 'CAT2']).strip()
            if cat2 != current_cat and cat2.lower() != 'nan':
                current_cat = cat2
                matched_img = None
                for key, img_path in description_image_info_dict.items():
                    if key.lower() == cat2.lower():
                        matched_img = img_path
                        break
                
                if matched_img and os.path.exists(matched_img):
                    desc_slide = prs.slides.add_slide(prs.slide_layouts[6])
                    desc_slide.shapes.add_picture(matched_img, Inches(0), Inches(0), prs.slide_width, prs.slide_height)

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
            item_df_full = item_df
            if 'fab_lot_id' in item_df.columns:
                target_df = item_df[item_df['fab_lot_id'] == target_lot_id]
                if len(target_df) == 0 and 'mask' in item_df.columns:
                    target_df = item_df[item_df['mask'] == main_vehicle]
                if len(target_df) == 0:
                    target_df = item_df
            else:
                target_df = item_df
            item_df = target_df

            measured_wafers = sorted(item_df[w_col].unique(), key=lambda x: int(x) if str(x).isdigit() else x)
            grouped = item_df.groupby(w_col)[item_name]

            # ---- 스펙 범위 추출 (Spec Limits) + 방향/로그/단위 ----
            spec_low = spec_data.loc[item_name, "SPECLOW"]
            spec_high = spec_data.loc[item_name, "SPECHIGH"]
            if pd.isna(spec_low): spec_low = None
            if pd.isna(spec_high): spec_high = None

            # REPORT DIRECTION: UPPER=상한만, LOWER=하한만, BOTH=둘 다
            direction = 'BOTH'
            if 'REPORT DIRECTION' in spec_data.columns:
                _d = str(spec_data.loc[item_name, 'REPORT DIRECTION']).strip().upper()
                if _d in ('UPPER', 'LOWER', 'BOTH'):
                    direction = _d
            if direction == 'UPPER':
                spec_low = None
            elif direction == 'LOWER':
                spec_high = None

            # REPORT LOG SCALE: 값 축 log10 적용 여부
            log_scale = False
            if 'REPORT LOG SCALE' in spec_data.columns:
                _ls = spec_data.loc[item_name, 'REPORT LOG SCALE']
                log_scale = str(_ls).strip().lower() in ('true', '1', '1.0', 'yes')

            # UNIT: 축 라벨 단위
            unit = ''
            if 'UNIT' in spec_data.columns:
                _u = str(spec_data.loc[item_name, 'UNIT']).strip()
                if _u and _u.lower() != 'nan':
                    unit = _u
            y_label = f"{item_name} [{unit}]" if unit else item_name

            slide = prs.slides.add_slide(prs.slide_layouts[6])
            
            # 슬라이드 배경색 설정 (흰색)
            slide_bg = slide.background
            slide_bg.fill.solid()
            slide_bg.fill.fore_color.rgb = RGBColor(255, 255, 255)
            
            # ---- 레이아웃 상수 (좌/우 2열, 타이틀↔그림 정렬 통일) ----
            LX, RX = 0.25, 8.55          # 좌/우 열 X 좌표 (테이블·그림·타이틀 공통)
            LW, RW = 8.0, 4.55           # 좌/우 열 폭
            TITLE_GAP = 0.25             # 타이틀과 바로 아래 그림 사이 간격
            # 그림 top Y 좌표 (좌열: 통계표/레전드/Box/WFMAP, 우열: Trend/Radius/CDF)
            Y_TABLE, Y_LEG, Y_BOX, Y_MAP = 0.80, 2.05, 2.55, 4.75
            Y_TREND, Y_RAD, Y_CUM = 1.05, 3.05, 5.05

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
            if reformatter is not None and item_name in reformatter['ALIAS'].values:
                row = reformatter[reformatter['ALIAS'] == item_name].iloc[0]
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
            add_card_title("■ Time-series Trend", RX, Y_TREND)
            add_card_title("■ Radial Distribution", RX, Y_RAD)
            add_card_title("■ Cumulative Distribution Function (CDF)", RX, Y_CUM)

            # ---- 2. Statistical Table (통계 테이블: #1~25 항상 고정 컬럼) ----
            cols = 27 # Stat + Total + #1 ~ #25 고정
            table_shape = slide.shapes.add_table(5, cols, Inches(LX), Inches(Y_TABLE), Inches(LW), Inches(1.2)).table
            
            # 헤더
            headers = ["Stat", "Total"] + [f"#{w}" for w in range(1, 26)]
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
            
            row_labels = ["Mean", "Std", "Min", "Max"]
            for r_idx, label in enumerate(row_labels):
                table_shape.cell(r_idx+1, 0).text = label
                
            # Total 통계
            table_shape.cell(1, 1).text = f"{item_df[item_name].mean():.3f}"
            table_shape.cell(2, 1).text = f"{item_df[item_name].std():.3f}" if len(item_df) > 1 else "-"
            table_shape.cell(3, 1).text = f"{item_df[item_name].min():.3f}"
            table_shape.cell(4, 1).text = f"{item_df[item_name].max():.3f}"
            
            # Wafer별 통계 (1~25 고정 루프)
            for w_idx in range(1, 26):
                w_str = str(w_idx)
                c_idx = w_idx + 1
                if w_str in grouped.groups:
                    grp_data = grouped.get_group(w_str)
                    table_shape.cell(1, c_idx).text = f"{grp_data.mean():.3f}"
                    table_shape.cell(2, c_idx).text = f"{grp_data.std():.3f}" if len(grp_data) > 1 else "-"
                    table_shape.cell(3, c_idx).text = f"{grp_data.min():.3f}"
                    table_shape.cell(4, c_idx).text = f"{grp_data.max():.3f}"
                else:
                    for r_idx in range(1, 5):
                        table_shape.cell(r_idx, c_idx).text = "-"
                
            for r_idx, row in enumerate(table_shape.rows):
                if r_idx == 0: continue
                bg_color = RGBColor(245, 247, 250) if r_idx % 2 == 1 else RGBColor(255, 255, 255)
                for c_idx, cell in enumerate(row.cells):
                    w_num_str = str(c_idx - 1)
                    if c_idx > 1 and w_num_str not in grouped.groups:
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

            # ---- 차트 임시 파일 경로 설정 (JPEG 압축 적용하여 PPTX 용량 다이어트) ----
            tmp_box = f"tmp_box_{item_name}.jpg"
            tmp_map = f"tmp_map_{item_name}.jpg"
            tmp_trend = f"tmp_trend_{item_name}.jpg"
            tmp_rad = f"tmp_rad_{item_name}.jpg"
            tmp_cum = f"tmp_cum_{item_name}.jpg"
            tmp_leg = f"tmp_leg_{item_name}.jpg"
            
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

            # Wafer 색상 고정 매핑
            cmap = plt.get_cmap('tab20')
            w_colors = {str(i): cmap((i-1)/25) for i in range(1, 26)}

            # ---- Wafer Color Legend ----
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

            _spec_lbl = 'Spec Limit'
            if spec_low is not None:
                ax_box.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl); _spec_lbl = None
            if spec_high is not None:
                ax_box.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl)
            ax_box.set_xticks(range(1, 26))
            ax_box.set_xticklabels([f"#{i}" for i in range(1, 26)])
            ax_box.set_xlim(0.5, 25.5)
            ax_box.tick_params(axis='x', rotation=45, labelsize=7)
            _label_axes(ax_box, xlabel="Wafer #", ylabel=y_label)
            if log_scale: ax_box.set_yscale('log')
            if spec_low is not None or spec_high is not None:
                ax_box.legend(fontsize=6, loc='best', frameon=False)
            _remove_spines(ax_box)
            ax_box.set_axisbelow(True)
            ax_box.grid(True, which='both', axis='both', color=C_GRID, linestyle='-', linewidth=0.5)
            # 용량 다이어트를 위한 JPG 포맷 저장 및 quality 옵션 적용
            fig_box.savefig(tmp_box, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_box)
            slide.shapes.add_picture(tmp_box, Inches(LX), Inches(Y_BOX), Inches(LW), Inches(1.9))

            # ---- 4. WF MAP (PGM/Wafer 다중 분할, 모자이크 타일 방식 적용, 3행 기준 고정 비율) ----
            # Wafer 좌표: flat-zone 회전이 반영된 보정 좌표(CHIP_X_ADJ/CHIP_Y_ADJ)가
            # 있으면 우선 사용해 실제 웨이퍼 배치대로 그린다. 없으면 raw chip_x/y로 fallback.
            map_x = 'CHIP_X_ADJ' if 'CHIP_X_ADJ' in item_df.columns else col_x
            map_y = 'CHIP_Y_ADJ' if 'CHIP_Y_ADJ' in item_df.columns else col_y

            sub_groups = list(item_df.groupby(col_sub))
            n_pgm = len(sub_groups) if len(sub_groups) > 0 else 1
            n_waf = 25

            global_x_min = item_df[map_x].min()
            global_x_max = item_df[map_x].max()
            global_y_min = item_df[map_y].min()
            global_y_max = item_df[map_y].max()

            x_range = global_x_max - global_x_min if global_x_max > global_x_min else 1
            y_range = global_y_max - global_y_min if global_y_max > global_y_min else 1
            global_x_min -= x_range * 0.05
            global_x_max += x_range * 0.05
            global_y_min -= y_range * 0.05
            global_y_max += y_range * 0.05

            global_vmin = item_df[item_name].min()
            global_vmax = item_df[item_name].max()

            # figsize 높이 비율을 행 갯수에 맞춤 (행당 0.6인치)
            fig_map, axes_map = plt.subplots(n_pgm, n_waf, figsize=(7.8, n_pgm * 0.6), squeeze=False, gridspec_kw={'wspace':0.05, 'hspace':0.05})
            sc = None

            for i, (sub_name, sub_grp) in enumerate(sub_groups):
                for j in range(25):
                    ax = axes_map[i, j]
                    w = str(j + 1)
                    if w in measured_wafers:
                        w_grp = sub_grp[sub_grp[w_col] == w]
                        if not w_grp.empty:
                            sc = ax.scatter(w_grp[map_x], w_grp[map_y], c=w_grp[item_name], cmap=GLOBAL_CONFIG.plot_cmap,
                                            vmin=global_vmin, vmax=global_vmax, s=40, marker='s', alpha=1.0)
                    ax.set_xticks([])
                    ax.set_yticks([])
                    ax.set_facecolor('#f8f9fa')
                    ax.set_aspect('equal', adjustable='box')  # 웨이퍼 왜곡 방지(정원형 유지)
                    for spine in ax.spines.values():
                        spine.set_visible(False)
                    ax.set_xlim(global_x_min, global_x_max)
                    ax.set_ylim(global_y_min, global_y_max)

                    if i == n_pgm - 1:
                        ax.set_xlabel(f"#{w}", fontsize=6, labelpad=2, color=C_NEUTRAL)
                    if j == 0:
                        ax.set_ylabel(str(sub_name), fontsize=6, rotation=-90, labelpad=8, color=C_NEUTRAL)

            if sc:
                cbar_ax = fig_map.add_axes([0.92, 0.1, 0.02, 0.8])
                cbar = fig_map.colorbar(sc, cax=cbar_ax)
                cbar.ax.tick_params(labelsize=6)
                cbar.set_label(y_label, fontsize=6, color=C_NAVY)  # 컬러바에 항목명/단위 표기
            fig_map.savefig(tmp_map, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': map_q})
            plt.close(fig_map)
            
            h_row = 2.35 / 3.0
            pic_height = n_pgm * h_row
            slide.shapes.add_picture(tmp_map, Inches(LX), Inches(Y_MAP), Inches(LW), Inches(pic_height))

            # ---- 5. Trend Chart (우측 상단 - vehicle/with_vehicle/target 비교 + vehicle 1~99% 구름대) ----
            import matplotlib.dates as mdates
            tdf = item_df_full.copy()
            tdf['tkout_time'] = pd.to_datetime(tdf['tkout_time'])
            has_mask = 'mask' in tdf.columns
            has_lot = 'fab_lot_id' in tdf.columns
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
                        ax.fill_between(roll.index, roll['q01'], roll['q99'], color=C_BAND, alpha=0.6, label='Vehicle 1~99%', zorder=1)
                        ax.plot(roll.index, roll['median'], color=C_NEUTRAL, linewidth=1.3, alpha=1.0, zorder=1.5)
                # scatter 겹침 순서: vehicle(초록) → with_vehicle(회색) → target lot(빨강, 최상단)
                # 모든 마커는 얇은 검정 테두리(edgecolors='black', linewidths 얇게)를 적용
                if has_lot:
                    if has_mask:
                        veh_other = tdf[(tdf['mask'] == main_vehicle) & (tdf['fab_lot_id'] != target_lot_id)]
                        wv = tdf[tdf['mask'] != main_vehicle]
                    else:
                        veh_other = tdf[tdf['fab_lot_id'] != target_lot_id]; wv = tdf.iloc[0:0]
                    tgt = tdf[tdf['fab_lot_id'] == target_lot_id]
                    if len(veh_other) > 0:
                        ax.scatter(veh_other['tkout_time'], veh_other[item_name], s=10, alpha=0.5, color=C_VEHICLE, label='Vehicle', edgecolors='black', linewidths=0.3, zorder=2)
                    if len(wv) > 0:
                        ax.scatter(wv['tkout_time'], wv[item_name], s=10, alpha=0.5, color=C_WV, label='With-Vehicle', edgecolors='black', linewidths=0.3, zorder=3)
                    if len(tgt) > 0:
                        # target lot(=리포팅 대상 lot id) 데이터: 빨간색 + 최상단(zorder 높게)로 강조
                        ax.scatter(tgt['tkout_time'], tgt[item_name], s=24, alpha=1.0, color='red', label='Target Lot', edgecolors='black', linewidths=0.4, zorder=10)
                else:
                    for w in measured_wafers:
                        grp = tdf[tdf[w_col] == w] if w_col in tdf.columns else tdf.iloc[0:0]
                        ax.scatter(grp['tkout_time'], grp[item_name], s=10, alpha=0.7, color=w_colors.get(str(w), 'blue'), edgecolors='black', linewidths=0.3, zorder=2)
                # spec line(s) — 방향(REPORT DIRECTION) 반영된 spec_low/high
                _sl = 'Spec Limit'
                if spec_low is not None:
                    ax.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_sl); _sl = None
                if spec_high is not None:
                    ax.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_sl)
                ax.set_title("")
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
                ax.tick_params(axis='x', rotation=0, labelsize=7)
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=6))
                _label_axes(ax, xlabel="Date", ylabel=y_label)
                if log_scale: ax.set_yscale('log')
                ax.legend(fontsize=6, loc='best', frameon=False)
                _remove_spines(ax)
                ax.grid(True, which='both', color=C_GRID, linestyle='-', linewidth=0.5)

            fig_trend, ax_trend = plt.subplots(figsize=(4.55, 1.75))
            _draw_trend(ax_trend)
            fig_trend.savefig(tmp_trend, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_trend)
            slide.shapes.add_picture(tmp_trend, Inches(RX), Inches(Y_TREND), Inches(RW), Inches(1.75))

            # index(alias) Trend scatter 차트만 RUN/TEMP에 alias명.png로 저장 (Anomaly/HTML 재사용)
            try:
                fig_trend_png, ax_trend_png = plt.subplots(figsize=(4.55, 2.0))
                _draw_trend(ax_trend_png)
                fig_trend_png.savefig(f"RUN/TEMP/{item_name}.png", dpi=100, bbox_inches="tight")
                plt.close(fig_trend_png)
            except Exception as e:
                print(f"[WARN] Failed to save RUN/TEMP/{item_name}.png: {e}")

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
                
                s_outs = []
                for _, row in t_df.iterrows():
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
                ax_rad.scatter(w_df[col_rad], w_df[item_name], s=12, alpha=0.85, color=p_color) 
                if len(w_df) >= 4:
                    try:
                        z = np.polyfit(w_df[col_rad], w_df[item_name], 3)
                        p = np.poly1d(z)
                        ax_rad.plot(np.linspace(w_df[col_rad].min(), w_df[col_rad].max(), 50), 
                                   p(np.linspace(w_df[col_rad].min(), w_df[col_rad].max(), 50)), 
                                   color=p_color, alpha=0.6, linewidth=1.5)
                    except: pass
            _spec_lbl = 'Spec Limit'
            if spec_low is not None:
                ax_rad.axhline(y=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl); _spec_lbl = None
            if spec_high is not None:
                ax_rad.axhline(y=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl)
            ax_rad.set_title("")
            _label_axes(ax_rad, xlabel="Chip Radius", ylabel=y_label)
            if log_scale: ax_rad.set_yscale('log')
            if spec_low is not None or spec_high is not None:
                ax_rad.legend(fontsize=6, loc='best', frameon=False)
            _remove_spines(ax_rad)
            ax_rad.grid(True, which='both', color=C_GRID, linestyle='-', linewidth=0.5)
            fig_rad.savefig(tmp_rad, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_rad)
            slide.shapes.add_picture(tmp_rad, Inches(RX), Inches(Y_RAD), Inches(RW), Inches(1.75))

            # ---- 7. Cumulative Plot (누적 분포) ----
            fig_cum, ax_cum = plt.subplots(figsize=(4.55, 1.85))
            for w in fixed_wafers:
                if w in grouped.groups:
                    grp = grouped.get_group(w)
                    sorted_data = np.sort(grp.values)
                    yvals = np.arange(len(sorted_data)) / float(len(sorted_data) - 1) if len(sorted_data) > 1 else [1.0]
                    ax_cum.plot(sorted_data, yvals, marker='.', linestyle='none', markersize=5, color=w_colors.get(str(w), 'blue'), alpha=0.8) 
            _spec_lbl = 'Spec Limit'
            if spec_low is not None:
                ax_cum.axvline(x=float(spec_low), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl); _spec_lbl = None
            if spec_high is not None:
                ax_cum.axvline(x=float(spec_high), color=C_ACCENT, ls="--", lw=1.2, alpha=0.7, label=_spec_lbl)
            ax_cum.set_title("")
            _label_axes(ax_cum, xlabel=y_label, ylabel="Cumulative Prob.")
            if log_scale: ax_cum.set_xscale('log')
            if spec_low is not None or spec_high is not None:
                ax_cum.legend(fontsize=6, loc='best', frameon=False)
            _remove_spines(ax_cum)
            ax_cum.grid(True, which='both', color=C_GRID, linestyle='-', linewidth=0.5)
            fig_cum.savefig(tmp_cum, format='jpg', dpi=dpi, bbox_inches="tight", facecolor='white', pil_kwargs={'quality': jpg_q})
            plt.close(fig_cum)
            slide.shapes.add_picture(tmp_cum, Inches(RX), Inches(Y_CUM), Inches(RW), Inches(1.85))

            # ---- 임시 차트 이미지 정리 ----
            for f in [tmp_box, tmp_map, tmp_trend, tmp_rad, tmp_cum, tmp_leg]:
                if os.path.exists(f): os.remove(f)
            gc.collect()

        except Exception as e:
            import traceback
            print(f"[ERROR] {item_name} 차트 생성 중 에러 발생: {e}")
            traceback.print_exc()

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
