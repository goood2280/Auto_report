# -*- coding: utf-8 -*-
"""
anomaly_engine - 코드 기반 Commonality / 이상 해석 엔진
======================================================

ET 측정 데이터에서 이상 항목을 감지하고, 엔지니어가 여러 확인 로직을
수동으로 돌리지 않아도 1차 자동 해석(Finding)을 코드만으로 산출합니다.
**AI(GPT) 없이도 동작**하며, AI가 켜져 있으면 이 Finding을 입력으로
자연어 서술을 보강합니다(2차).

주요 기능:
    - analyze_commonality(): 각 Index 항목별 '한 개'의 이상 Finding 산출
        · spec-out(CRITICAL) → 이탈 개수/최대 이탈값
        · spec 미초과 시 median 이탈(σ) 또는 std 산포 확대(배수) 중 하나 (WARNING)
        ※ 불량 모드(조합 해석)는 코드가 추정하지 않고, AI + ANOMALY_KNOWLEDGE.md('불량 모드 판정표')에 위임
    - render_findings_html(): Finding 리스트 → HTML(<ul>, severity별 색상)
    - run_anomaly_pipeline(): (구) Z-Score Trend 차트 — 하위호환용 유지

임계값/룰셋은 My_config.py에서 조정합니다.

사용법:
    from anomaly_engine import analyze_commonality, render_findings_html
    findings = analyze_commonality(merged_df, target_lot_id, metrics_dict,
                                   spec_data, main_vehicle=vehicle, config=GLOBAL_CONFIG)
    html = render_findings_html(findings)
"""

# ===================================================================
#  표준·서드파티 임포트 (Standard & Third-party Imports)
# ===================================================================
import re
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import base64
from io import BytesIO

# Matplotlib 백엔드를 Agg(비-GUI)로 설정하여 서버 환경 호환
matplotlib.use('Agg')


# ===================================================================
#  메인 파이프라인 함수 (Main Pipeline Function)
# ===================================================================

def run_anomaly_pipeline(merged_df, root_lot_id, dc_step,
                         spec_data=None, reformatter=None,
                         config=None, **kwargs):
    """이상탐지 파이프라인을 실행하고 결과를 반환합니다 (Mock 구현).

    Z-Score 기반으로 이상 항목을 탐지한 뒤, 감지된 항목에 대해
    Trend 차트(3열 × 2행)를 생성하여 Base64 인코딩된 HTML 이미지로 반환합니다.

    Parameters
    ----------
    merged_df : pd.DataFrame
        피벗 완료된 ET 측정 데이터 (컬럼 = 항목명, 행 = 측정 포인트).
    root_lot_id : str
        대상 루트 Lot ID (로깅·제목 표시용).
    dc_step : str
        대상 DC Step 이름.
    spec_data : pd.DataFrame, optional
        스펙 정보 DataFrame. 현재 Mock에서는 사용하지 않으나
        실제 엔진 교체 시 스펙 기반 판정에 활용됩니다.
    reformatter : pd.DataFrame, optional
        reformatter 설정 DataFrame. 실제 엔진 교체 시 활용.
    config : object, optional
        설정 객체. 아래 속성(attribute)들을 통해 동작을 제어합니다.
        지정하지 않으면(config=None) 모든 파라미터에 기본값이 적용됩니다.

        - ``anomaly_z_threshold`` (float): Z-Score 임계값. 기본 3.0.
        - ``max_anomaly_chart_items`` (int): 차트에 표시할 최대 이상 항목 수. 기본 6.
        - ``anomaly_chart_figsize`` (tuple): 차트 전체 크기 (width, height). 기본 (10, 4.0).
        - ``anomaly_chart_dpi`` (int): 차트 저장 DPI. 기본 120.
    **kwargs
        추가 키워드 인자 (향후 확장용, 현재 미사용).

    Returns
    -------
    dict
        - ``html`` (str): 이상 항목 Trend 차트가 포함된 HTML 문자열.
          이상 항목이 없으면 "이상항목 없음" 텍스트.
        - ``anomaly_items`` (list[dict]): 감지된 이상 항목 목록.
          각 항목: ``{"item", "max_z", "mean", "std"}``.
        - ``summary`` (str): 요약 문자열 (예: "3개 이상항목 감지").

    Examples
    --------
    >>> result = run_anomaly_pipeline(df, "LOT001", "STEP_A")
    >>> print(result["summary"])
    "2개 이상항목 감지"

    Notes
    -----
    ``config=None`` 이면 모든 파라미터에 기본값이 적용되므로,
    기존 코드에서 config 없이 호출해도 동작이 동일합니다 (하위 호환성 보장).
    """

    # -----------------------------------------------------------------
    # 1. config에서 설정값 읽기 (Read settings from config with fallbacks)
    #    config가 None이면 기본값을 사용하여 하위 호환성 유지
    # -----------------------------------------------------------------
    z_threshold = getattr(config, 'anomaly_z_threshold', 3.0) if config else 3.0
    max_items = getattr(config, 'max_anomaly_chart_items', 6) if config else 6
    figsize = getattr(config, 'anomaly_chart_figsize', (10, 4.0)) if config else (10, 4.0)
    chart_dpi = getattr(config, 'anomaly_chart_dpi', 120) if config else 120

    print(f"[MOCK-anomaly] run_anomaly_pipeline: {root_lot_id} / {dc_step}")
    print(f"[MOCK-anomaly] 설정 - z_threshold={z_threshold}, max_items={max_items}, "
          f"figsize={figsize}, dpi={chart_dpi}")

    # -----------------------------------------------------------------
    # 2. Z-Score 기반 이상 항목 탐지 (Anomaly Detection via Z-Score)
    # -----------------------------------------------------------------
    anomaly_items = []
    try:
        # 측정 항목이 아닌 메타 컬럼은 탐지 대상에서 제외
        skip = {
            "CHIP_X_POS", "CHIP_Y_POS", "WAFER_ID", "FLAT_ZONE_POS",
            "TEMPERATURE", "CHIP_X_ADJ", "CHIP_Y_ADJ", "Chip_Radius",
            "Duplicate_Count", "Point"
        }
        # 숫자형 컬럼만 대상으로 순회
        numeric_cols = merged_df.select_dtypes(include=["float64", "int64"]).columns
        for col in numeric_cols:
            if col in skip:
                continue
            s = merged_df[col].dropna()
            # 데이터 포인트가 3개 미만이면 통계 판정 불가 → 건너뜀
            if len(s) < 3:
                continue
            mean_val = s.mean()
            std_val = s.std()
            if std_val > 0:
                # 각 데이터 포인트의 Z-Score 절대값 중 최대값 계산
                z_max = ((s - mean_val) / std_val).abs().max()
                # z_threshold 초과 시 이상 항목으로 등록
                if z_max > z_threshold:
                    anomaly_items.append({
                        "item": col,
                        "max_z": round(float(z_max), 2),
                        "mean": round(float(mean_val), 4),
                        "std": round(float(std_val), 4),
                    })
    except Exception as e:
        print(f"[MOCK-anomaly] 이상 탐지 중 에러 발생: {e}")

    # -----------------------------------------------------------------
    # 3. 이상 항목 Trend 차트 생성 (Anomaly Trend Chart Generation)
    #    - 3열 × 2행(cols=3, rows=2) 그리드
    #    - 최대 max_items개 항목만 차트에 표시
    # -----------------------------------------------------------------
    if anomaly_items:
        # 최대 max_items개 항목만 선택
        target_items = [a['item'] for a in anomaly_items[:max_items]]
        n_items = len(target_items)

        # 차트 그리드 레이아웃: 고정 3열 × 2행
        cols = 3
        rows = 2

        fig, axes = plt.subplots(rows, cols, figsize=figsize)
        axes = axes.flatten()

        for i in range(len(axes)):
            ax_trend = axes[i]

            if i < n_items:
                item_name = target_items[i]
                if item_name not in merged_df.columns:
                    ax_trend.axis('off')
                    continue

                item_df = merged_df.dropna(subset=[item_name]).copy()
                if len(item_df) == 0:
                    ax_trend.axis('off')
                    continue

                # --- 시계열 데이터가 있는 경우: Trend 차트 ---
                if 'tkout_time' in item_df.columns and item_df['tkout_time'].notna().any():
                    item_df['tkout_time'] = pd.to_datetime(item_df['tkout_time'])
                    trend_df = item_df[['tkout_time', item_name]].sort_values('tkout_time')
                    trend_df['date'] = trend_df['tkout_time'].dt.date

                    # 일별 통계 계산 (중앙값, 1%·99% 분위수)
                    daily_stats = trend_df.groupby('date')[item_name].agg(
                        median='median',
                        q01=lambda x: x.quantile(0.01),
                        q99=lambda x: x.quantile(0.99)
                    ).reset_index()
                    daily_stats['date'] = pd.to_datetime(daily_stats['date'])
                    daily_stats = daily_stats.set_index('date').sort_index()

                    # 3일 이동평균으로 구름대(band) 생성
                    roll_stats = daily_stats.rolling('3D', min_periods=1).mean()
                    ax_trend.fill_between(
                        roll_stats.index,
                        roll_stats['q01'], roll_stats['q99'],
                        color='gray', alpha=0.2
                    )
                    # 중앙값 라인 (marker='s': 사각형 마커로 단일 점도 표시)
                    ax_trend.plot(
                        roll_stats.index, roll_stats['median'],
                        marker='s', markersize=4,
                        color='blue', linewidth=1.5, alpha=0.6
                    )

                    # wafer_id 컬럼 대소문자 호환 처리
                    w_col = ('wafer_id' if 'wafer_id' in item_df.columns
                             else 'WAFER_ID' if 'WAFER_ID' in item_df.columns
                             else None)
                    if w_col:
                        for w in item_df[w_col].unique():
                            grp = item_df[item_df[w_col] == w]
                            ax_trend.scatter(
                                grp['tkout_time'], grp[item_name],
                                s=12, alpha=0.7, label=w
                            )
                else:
                    # --- 시계열 없음: 인덱스 기반 라인 차트 ---
                    ax_trend.plot(
                        range(len(item_df)), item_df[item_name],
                        marker='o', markersize=4,
                        linestyle='-', color='blue', alpha=0.6
                    )

                # 차트 제목 및 축 설정
                ax_trend.set_title(
                    f"Trend: {item_name}",
                    fontsize=10, fontweight='bold', pad=3
                )
                ax_trend.tick_params(axis='x', rotation=15, labelsize=7)
                ax_trend.tick_params(axis='y', labelsize=7)
                ax_trend.grid(axis='y', linestyle=':', alpha=0.6)
            else:
                # 남은 빈 셀은 숨김 처리
                ax_trend.axis('off')

        # 레이아웃 정리 후 Base64 인코딩된 PNG로 변환
        plt.tight_layout()
        buf = BytesIO()
        fig.savefig(buf, format='png', dpi=chart_dpi,
                    bbox_inches='tight', transparent=True)
        buf.seek(0)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        plt.close(fig)

        # HTML 이미지 태그로 래핑
        html = (
            f'<div style="margin-top: 10px; margin-bottom: 20px; text-align: left;">'
            f'<img src="data:image/png;base64,{img_b64}" '
            f'style="max-width: 95%; border: 1px solid #d2d6dc; border-radius: 4px;">'
            f'</div>'
        )
    else:
        # 이상 항목 미감지 시 텍스트 메시지 반환
        html = (
            '<p style="font-size:14px; color:#333; margin-top:5px; '
            'margin-bottom:15px; margin-left:20px;">'
            '이상항목 없음 (Mock Engine)</p>'
        )

    # -----------------------------------------------------------------
    # 4. 결과 요약 및 반환 (Result Summary & Return)
    # -----------------------------------------------------------------
    summary = (f"{len(anomaly_items)}개 이상항목 감지"
               if anomaly_items else "이상항목 없음")

    return {"html": html, "anomaly_items": anomaly_items, "summary": summary}


# ===================================================================
#  코드 기반 Commonality / 이상 해석 엔진 (AI 없이 동작)
# ===================================================================

# ──────────────────────────────────────────────────────────────────────
# Finding severity 등급 — 신호등 3색 (HTML/PPT 색상·정렬에 사용)
#   CRITICAL : 빨강 ● 이상            (spec-out 불량모드 등 확정적 이상)
#   WARNING  : 주황 ● 주의            (공정 실이상이나 재현성 낮음 — std 확대/집단분리/median 이탈/drift/재발)
#   NOTICE   : 노랑 ● 주의            (측정이상 추정 — PCHK 동일 site 등 측정 신뢰성 의심)
#   INFO     : 회색 ● 참고
# ※ 색/라벨을 바꾸려면 이 표만 수정하면 됨(HTML·요약 head 모두 반영). PPT는 insert_findings_page 미러.
# ※ 각 finding이 어느 등급인지는 analyze_commonality의 _finding(...) 첫 인자로 결정.
# ──────────────────────────────────────────────────────────────────────
_SEV_ORDER = {"CRITICAL": 0, "WARNING": 1, "NOTICE": 2, "INFO": 3}
_SEV_COLOR = {"CRITICAL": "#d62728", "WARNING": "#f59e0b", "NOTICE": "#eab308", "INFO": "#5d6d7e"}
_SEV_LABEL = {"CRITICAL": "이상", "WARNING": "주의", "NOTICE": "주의", "INFO": "참고"}
# 요약 head에서 색별 의미를 알려주는 짧은 이름(라벨과 별개)
_SEV_HEAD = {"CRITICAL": "이상", "WARNING": "주의", "NOTICE": "측정이상 추정", "INFO": "참고"}


def _sev_dot(sev):
    """severity → 신호등 원(●) HTML(색상만)."""
    return f'<span style="color:{_SEV_COLOR.get(sev, "#5d6d7e")};">&#9679;</span>'


def _sev_badge(sev):
    """severity → 신호등 원(●) + 라벨(검정) HTML. 예: 🔴 이상 / 🟠 주의 / 🟡 주의"""
    return f'{_sev_dot(sev)} <b style="color:#1a1a1a;">{_SEV_LABEL.get(sev, sev)}</b>'


def _pick_col(df, *names):
    """컬럼명을 대소문자/후보 순으로 탐색 (없으면 None)."""
    for n in names:
        if n in df.columns:
            return n
    return None


def item_excluded(name, patterns):
    """통계 자동분석 제외 판정.

    My_config.anomaly_exclude_items(패턴 리스트)에 걸리면 True. 대소문자 무시,
    fnmatch 와일드카드(*, ?) 지원. 예: 'MAWIN_*' → MAWIN_minus_margin 등 전부 제외.
    """
    if not patterns:
        return False
    import fnmatch
    s = str(name).upper()
    return any(fnmatch.fnmatch(s, str(p).upper()) for p in patterns)


def _finding(sev, ftype, item, title, detail=""):
    return {"severity": sev, "type": ftype, "item": item, "title": title, "detail": detail}


def _convert_name(x, prefixes=None, suffixes=None, repl=None):
    """ALIAS(원 아이템명) → HTML/PPT 표시명 변환(My_Function.convert_target_data와 동일 규칙).

    접두 제거 → 접미 제거 → replace_map 치환. 표시명 매칭용으로 anomaly_engine이
    My_Function에 의존하지 않도록 같은 로직을 복제한다.
    """
    if not isinstance(x, str):
        return x
    for p in (prefixes or []):
        if p and x.startswith(p):
            x = x[len(p):]
    for s in (suffixes or []):
        if s and x.endswith(s):
            x = x[: -len(s)]
    for o, n in (repl or {}).items():
        x = x.replace(o, n)
    return x


def _parse_pchk_item_map(text):
    """ANOMALY_KNOWLEDGE.md에서 'PCHK → 검증 대상 ITEM' 매핑을 파싱.

    형식(마커 사이 우선, 없으면 전체 스캔):
        <!-- PCHK_ITEM_MAP:start -->
        - PCHK_LKG: VTH_N, VTH_P, IDSAT_N
        - PCHK_Res: IDSAT_RATIO, RMAX_VTH
        <!-- PCHK_ITEM_MAP:end -->

    반환: { 'PCHK_LKG': ['VTH_N','VTH_P','IDSAT_N'], ... }  (키는 원문 표기 유지).
    항목이 비면 매핑 없음으로 간주(해당 PCHK는 모든 spec-out 항목과 대조).
    """
    import re
    out = {}
    if not text:
        return out
    body = text
    _s = text.find('PCHK_ITEM_MAP:start')
    _e = text.find('PCHK_ITEM_MAP:end')
    if _s != -1 and _e != -1 and _e > _s:
        body = text[_s:_e]
    for line in body.splitlines():
        if 'PCHK_ITEM_MAP' in line:   # 마커(:start/:end) 라인 제외
            continue
        # 마커 사이의 '- <PCHK 표시명>: ITEM1, ITEM2' 라인. PCHK명은 임의 형식 허용
        # (예: 'RMAX(PCHK Lkg)'). 콜론 앞 전체를 키로 사용.
        mt = re.match(r'\s*[-*]\s*([^:：]+?)\s*[:：]\s*(.+)$', line)
        if not mt:
            continue
        key = mt.group(1).strip()
        items = [t.strip() for t in mt.group(2).split(',') if t.strip()]
        if items:
            out[key] = items
    return out


def _parse_knowledge_rules(text):
    """ANOMALY_KNOWLEDGE.md의 판정 로직 규칙(ANOMALY_RULES 마커 사이)을 파싱.

    한 규칙 = RULE:(라벨) + WHEN:(조건) + [LEVEL/LINK/NOTE]. 규칙끼리는 새 RULE:로 구분.
    반환: [{'label','when','level','link','note'}, ...]
    """
    import re
    rules = []
    if not text:
        return rules
    _s = text.find('ANOMALY_RULES:start')
    _e = text.find('ANOMALY_RULES:end')
    if _s == -1 or _e == -1 or _e <= _s:
        return rules
    body = text[_s:_e]
    cur = None
    def _has_action(c):
        return bool(c) and (c.get('when') or c.get('suppress_disp') or c.get('compare_disp'))

    for line in body.splitlines():
        if 'ANOMALY_RULES' in line:      # 마커 라인 제외
            continue
        m = re.match(r'\s*(RULE|WHEN|LEVEL|LINK|NOTE|SUPPRESS_DISP|COMPARE_DISP)\s*[:：]\s*(.*)$',
                     line, re.IGNORECASE)
        if not m:
            continue
        key = m.group(1).upper()
        val = m.group(2).strip()

        if key == 'RULE':
            if _has_action(cur):
                rules.append(cur)
            cur = {'label': val, 'when': '', 'level': '주의', 'link': '', 'note': '',
                   'suppress_disp': None, 'compare_disp': None}
        elif cur is not None:
            if key == 'WHEN':
                cur['when'] = val
            elif key == 'LEVEL':
                cur['level'] = val
            elif key == 'LINK':
                cur['link'] = val
            elif key == 'NOTE':
                cur['note'] = val
            elif key == 'SUPPRESS_DISP':
                cur['suppress_disp'] = [t.strip() for t in val.split(',') if t.strip()]
            elif key == 'COMPARE_DISP':
                # "A,B | D,E" → 두 그룹
                _parts = val.split('|')
                if len(_parts) == 2:
                    cur['compare_disp'] = (
                        [t.strip() for t in _parts[0].split(',') if t.strip()],
                        [t.strip() for t in _parts[1].split(',') if t.strip()])
    if _has_action(cur):
        rules.append(cur)
    return rules


def analyze_commonality(merged_df, target_lot_id, metrics_dict, spec_data,
                        main_vehicle=None, config=None, reformatter=None,
                        knowledge_text=""):
    """코드 기반 다중 detector로 이상/commonality Finding 리스트를 산출.

    AI 사용 여부와 무관하게 항상 코드로 동작합니다. 각 detector는 독립적으로
    try/except 처리되어 하나가 실패해도 나머지 분석은 계속됩니다.

    Parameters
    ----------
    merged_df : pd.DataFrame   전체(모든 lot, vehicle+with_vehicle) 피벗 데이터
    target_lot_id : str        리포팅 대상 fab_lot_id
    metrics_dict : dict        insert_plots가 계산한 항목별 통계
    spec_data : pd.DataFrame    ALIAS 인덱스, SPECLOW/SPECHIGH 보유
    main_vehicle : str          모집단 기준 vehicle 명(없으면 전체 사용)
    config : object             임계값/룰셋(My_config). 없으면 기본값

    Returns
    -------
    list[dict] : Finding 목록 (severity 순 정렬). 각 항목
        {severity, type, item, title, detail}
    """
    import numpy as np

    def cfg(k, d):
        return getattr(config, k, d) if config else d

    sigma_med = cfg('anomaly_lot_median_sigma', 2.0)
    disp_ratio = cfg('anomaly_lot_dispersion_ratio', 1.5)

    # radius zone 경계(Center ≤ r_center_max, Middle ≤ r_middle_max, 그 외 Edge).
    #  radius plot이 참고하는 설정파일(reformatter/config.yaml)의 radius_zones와 동일 값.
    _rz = [60, 100]
    try:
        if config is not None and hasattr(config, 'get'):
            _rz = config.get('radius_zones', [60, 100]) or [60, 100]
    except Exception:
        _rz = [60, 100]
    try:
        r_center_max, r_middle_max = float(_rz[0]), float(_rz[1])
    except Exception:
        r_center_max, r_middle_max = 60.0, 100.0

    findings = []
    if merged_df is None or len(merged_df) == 0 or not metrics_dict:
        return findings

    col_lot = _pick_col(merged_df, 'FAB_LOT_ID', 'fab_lot_id')
    col_waf = _pick_col(merged_df, 'WAFER_ID', 'wafer_id')
    col_time = _pick_col(merged_df, 'TKOUT_TIME', 'tkout_time')
    col_mask = _pick_col(merged_df, 'MASK', 'mask')
    col_x = _pick_col(merged_df, 'CHIP_X_ADJ', 'CHIP_X_POS', 'chip_x_pos')
    col_y = _pick_col(merged_df, 'CHIP_Y_ADJ', 'CHIP_Y_POS', 'chip_y_pos')
    col_pgm = _pick_col(merged_df, 'PGM(pt)')

    items = [it for it in metrics_dict.keys() if it in merged_df.columns]

    # 모집단: main_vehicle만 (lot간 비교용)
    pop = merged_df
    if col_mask and main_vehicle is not None:
        _m = merged_df[merged_df[col_mask] == main_vehicle]
        if len(_m) > 0:
            pop = _m
    tgt = pop[pop[col_lot] == target_lot_id] if col_lot else pop

    # spec dict {alias: (low, high)} — 차트 항목은 spec_data, PCHK 등 비차트 항목은 reformatter에서 보강
    spec = {}
    try:
        for it in items:
            if it in spec_data.index:
                lo = spec_data.loc[it, 'SPECLOW']
                hi = spec_data.loc[it, 'SPECHIGH']
                spec[it] = (float(lo) if pd.notna(lo) else None,
                            float(hi) if pd.notna(hi) else None)
        # reformatter 전체(REPORT ORDER 없는 PCHK 포함)에서 ALIAS별 spec 보강
        if reformatter is not None and 'ALIAS' in reformatter.columns:
            for _, r in reformatter.iterrows():
                a = r.get('ALIAS')
                if pd.isna(a) or a in spec:
                    continue
                lo = r.get('SPECLOW')
                hi = r.get('SPECHIGH')
                spec[a] = (float(lo) if pd.notna(lo) else None,
                           float(hi) if pd.notna(hi) else None)
    except Exception as e:
        print(f"[anomaly] spec dict 구성 실패: {e}")

    spec_out_items = [it for it in items if metrics_dict.get(it, {}).get('spec_out_count', 0) > 0]

    # REPORT ORDER (spec-out 동순위 최종 tie-break용). spec_data는 ALIAS 인덱스.
    report_order = {}
    try:
        if spec_data is not None and 'REPORT ORDER' in getattr(spec_data, 'columns', []):
            _ro = pd.to_numeric(spec_data['REPORT ORDER'], errors='coerce')
            for _idx, _v in _ro.items():
                if pd.notna(_v):
                    report_order[_idx] = float(_v)
    except Exception as e:
        print(f"[anomaly] REPORT ORDER 파싱 실패: {e}")

    from collections import defaultdict

    def _robust(series):
        """robust 중심(median)과 산포(1.4826*MAD, 0이면 IQR/1.349, 그래도 0이면 std)."""
        s = pd.to_numeric(series, errors='coerce').dropna()
        if len(s) == 0:
            return None, None
        med = float(s.median())
        mad = float((s - med).abs().median())
        spread = 1.4826 * mad
        if spread <= 0:
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            spread = float(q3 - q1) / 1.349
        if spread <= 0 and len(s) > 1:
            spread = float(s.std())
        return med, (spread if spread and spread > 0 else None)

    def _waf_int(v):
        try:
            return int(float(str(v).replace('#', '')))
        except Exception:
            return None

    def _specout_by_wafer(tgt_it, col, it, lo, hi):
        """target lot을 wafer별 (총 측정 pt, spec-out pt)로 그룹핑.

        반환: (텍스트, 총 spec-out개수, {spec-out pt개수: [wafer,...]}, 최고 wafer 비율, spec-out wafer 수).
        - 최고 wafer 비율 = max_w(spec-out pt_w / 측정 pt_w)  → spec-out 순위 1순위.
        - spec-out wafer 수 = spec-out이 하나라도 있는 wafer 개수 → 순위 2순위.
        텍스트 형식은 '총 몇pt 측정 중 몇pt out'을 함께 표기 — 예: "150pt 중 5pt out: #3, #7".
        """
        _v = pd.to_numeric(tgt_it[it], errors='coerce')
        _om = pd.Series(False, index=tgt_it.index)
        if lo is not None: _om = _om | (_v < lo)
        if hi is not None: _om = _om | (_v > hi)
        n_total = int(_om.sum())
        if n_total == 0:
            return '', 0, {}, 0.0, 0
        measured_by_w = tgt_it.groupby(col).size()          # wafer별 총 측정 pt
        out_by_w = tgt_it[_om.values].groupby(col).size()   # wafer별 spec-out pt
        # wafer별 spec-out 비율(out pt / 측정 pt): 최고 비율·그런 wafer 수를 순위 지표로 산출
        _ratios = [int(oc) / int(measured_by_w.get(w, oc))
                   for w, oc in out_by_w.items() if int(measured_by_w.get(w, oc)) > 0]
        max_ratio = max(_ratios) if _ratios else 0.0
        n_wafers = int((out_by_w > 0).sum())
        # (측정 pt, out pt)가 같은 wafer끼리 묶어 "{측정}pt 중 {out}pt out: #.." 표기
        by_key = defaultdict(list)
        for w, ocnt in out_by_w.items():
            mcnt = int(measured_by_w.get(w, ocnt))
            wi = _waf_int(w)
            by_key[(mcnt, int(ocnt))].append(wi if wi is not None else w)
        txt = ' / '.join(
            f"{m}pt 중 {o}pt out: "
            + ', '.join('#' + str(w) for w in sorted(by_key[(m, o)], key=lambda z: (z is None, z)))
            for (m, o) in sorted(by_key, key=lambda k: (k[0], k[1])))
        # 하위호환 map: {out pt개수: [wafer,...]}
        _cnt_map = defaultdict(list)
        for (m, o), ws in by_key.items():
            _cnt_map[o].extend(ws)
        return (txt, n_total,
                {int(o): sorted(v, key=lambda z: (z is None, z)) for o, v in _cnt_map.items()},
                max_ratio, n_wafers)

    # ---- 각 Index 항목별 '한 개'의 이상 finding (PCHK 포함 전 항목 동일 기준) ----
    #   유형(우선순위): spec-out(CRITICAL) > wafer median 이탈 > wafer 산포 확대 (WARNING).
    #   모든 비교는 target lot의 '각 wafer'를 제품 전체의 'wafer별 기준'과 대조한다:
    #     · spec-out : wafer별 (spec-out pt / 측정 pt) 비율 → 최고 비율·그런 wafer 수로 순위.
    #     · median   : wafer median이 제품 wafer median 분포에서 몇 σ(wafer간 산포 기준) 이탈.
    #     · 산포     : wafer 내부 robust 산포가 '보통 wafer 산포'의 몇 배.
    #   비교는 해당 vehicle 내로 한정(with_vehicle 제외).
    _veh = main_vehicle or '모집단'

    def _pop_wafer_baseline(it):
        """제품(pop) 전체를 (lot, wafer) 단위로 나눠 'wafer 기준' 3종을 산출.

        반환 (wafer_median_center, wafer_median_scatter, typ_wafer_spread):
        - wafer_median_center  : 제품 각 wafer median들의 중심(median).
        - wafer_median_scatter : 제품 각 wafer median들의 robust 산포(=wafer간 변동). median 이탈 σ의 분모.
        - typ_wafer_spread     : 제품 각 wafer 내부 robust 산포의 중앙값(='보통 wafer 산포'). 산포배수 분모.
        모두 '전체 lot의 wafer별' 통계 → target lot의 각 wafer를 이 기준과 비교한다.
        """
        if it not in pop.columns or not col_waf:
            return None, None, None
        _keys = [col_lot, col_waf] if col_lot else [col_waf]
        _wm, _ws = [], []
        for _k, _g in pop[_keys + [it]].dropna(subset=[it]).groupby(_keys):
            _m, _s = _robust(_g[it])
            if _m is not None:
                _wm.append(_m)
            if _s:
                _ws.append(_s)
        _center, _scatter = _robust(pd.Series(_wm)) if _wm else (None, None)
        _typ = float(pd.Series(_ws).median()) if _ws else None
        return _center, _scatter, _typ

    def _zone_of(r):
        if r <= r_center_max:
            return 'Center'
        if r <= r_middle_max:
            return 'Middle'
        return 'Edge'

    def _specout_extra(it, lo, hi):
        """spec-out chip의 PGM(pt) 목록과 radius zone(Center/Middle/Edge) 분포 반환."""
        cols = [c for c in [col_x, col_y, col_pgm, it] if c and c in tgt.columns]
        if it not in tgt.columns or not cols:
            return [], {}
        _sub = tgt[cols].dropna(subset=[it])
        if len(_sub) == 0:
            return [], {}
        _v = pd.to_numeric(_sub[it], errors='coerce')
        _om = pd.Series(False, index=_sub.index)
        if lo is not None: _om = _om | (_v < lo)
        if hi is not None: _om = _om | (_v > hi)
        _so = _sub[_om.values]
        if len(_so) == 0:
            return [], {}
        # PGM(pt) 뒤 Duplicate_Count 기본값('_1.0'/'_1') 접미사는 불필요 → 제거(중복>1은 유지)
        pgms = ([re.sub(r'_1(?:\.0+)?$', '', str(p)) for p in _so[col_pgm].dropna().unique()]
                if col_pgm and col_pgm in _so.columns else [])
        zones = {}
        if col_x and col_y and col_x in _so.columns and col_y in _so.columns:
            _r = np.sqrt(pd.to_numeric(_so[col_x], errors='coerce') ** 2 +
                         pd.to_numeric(_so[col_y], errors='coerce') ** 2)
            for z in _r.dropna().map(_zone_of):
                zones[z] = zones.get(z, 0) + 1
        return pgms, zones

    # PCHK 계열도 '동일한 index 항목'으로 같은 루프에서 함께 분석하고, 판정도 동일하게 적용한다.
    #   - 비차트(REPORT ORDER 없음)라 metrics_dict엔 없지만 merged_df엔 컬럼으로 존재 → items에 합류.
    #   - spec-out이면 다른 Index와 똑같이 '이상(CRITICAL)'으로 본다(별도 MEAS_SUSPECT 없음).
    #   - 단, '동일 shot 다른 항목 동시 spec-out' 겹침 신호는 basis에만 기록해 AI 측정이상 추정에 넘긴다.
    pchk_aliases = []
    try:
        if reformatter is not None and 'ALIAS' in reformatter.columns:
            for _, r in reformatter.iterrows():
                a = r.get('ALIAS')
                if pd.isna(a):
                    continue
                cat2 = str(r.get('CAT2', '')).upper()
                if (cat2 == 'PCHK' or str(a).upper().startswith('PCHK')) \
                        and a in merged_df.columns and a not in items:
                    pchk_aliases.append(a)
    except Exception as e:
        print(f"[anomaly] PCHK 목록 구성 실패: {e}")
    pchk_set = set(pchk_aliases)
    items = list(items) + pchk_aliases

    # ── 통계 자동분석 제외 항목(My_config.anomaly_exclude_items) 적용 ──
    #   여기서 걸러진 항목은 finding·basis·우선순위·Trend chart 어디에도 나오지 않는다.
    _excl = list(cfg('anomaly_exclude_items', []) or [])
    # WF MAP 제외 키워드(wfmap_exclude_keywords)에 해당하는 항목도 통계 이상/주의 판정에서 제외.
    #   키워드는 '부분일치'이므로 item_excluded(fnmatch)용 *KEYWORD* 패턴으로 변환.
    for _kw in (cfg('wfmap_exclude_keywords', []) or []):
        _kw = str(_kw).strip()
        if _kw:
            _excl.append(f"*{_kw}*")
    if _excl:
        _n0 = len(items)
        items = [it for it in items if not item_excluded(it, _excl)]
        pchk_set = {a for a in pchk_set if not item_excluded(a, _excl)}
        if len(items) < _n0:
            print(f"[anomaly] anomaly_exclude_items로 {_n0 - len(items)}개 항목 통계분석 제외")

    # ── PCHK 종류별 '검증 대상 ITEM' 매핑 (ANOMALY_KNOWLEDGE.md에서 관리) ──
    #   예) PCHK_LKG → [VTH_N, VTH_P, ...] : PCHK_LKG가 이 항목들과 동일 PGM(pt)·shot에서
    #       함께 spec-out일 때만 측정이상으로 본다. PCHK_Res는 다른 항목군.
    #   매핑에 적힌 ITEM 명은 Index ALIAS(원 이름)든 HTML/PPT 표시명(replace 적용)이든
    #   둘 다 인식한다(_name_forms). 매핑에 없는 PCHK는 모든 spec-out 항목과 대조(하위호환).
    pchk_item_map = _parse_pchk_item_map(knowledge_text)
    _repl = getattr(config, 'replace_map', {}) if config else {}
    _suf = getattr(config, 'suffixes_remove', []) if config else []
    _pre = getattr(config, 'prefixes_remove', []) if config else []

    def _name_forms(nm):
        """항목명의 인식 형태 집합 = {원 이름, 표시명}. 둘 중 하나만 겹쳐도 동일 항목."""
        if not isinstance(nm, str):
            return {nm}
        return {nm, _convert_name(nm, _pre, _suf, _repl)}

    def _disp(nm):
        """사용자에게 보여지는 표시명(접두/접미 제거·치환 후처리). 내부 키는 원 이름 유지."""
        return _convert_name(nm, _pre, _suf, _repl)

    def _resolve_allowed(pchk_alias, device_items):
        """PCHK의 검증 대상 ITEM(실제 alias) 집합. 매핑 없으면 None(전체 대조)."""
        toks = None
        _pf = _name_forms(pchk_alias)
        for k, v in pchk_item_map.items():
            if _name_forms(k) & _pf:
                toks = v
                break
        if toks is None:
            return None, None
        allowed = set()
        for t in toks:
            _tf = _name_forms(t)
            for d in device_items:
                if _name_forms(d) & _tf:
                    allowed.add(d)
        return allowed, toks

    def _outmask(frame, it):
        """spec(both-bound) 기준 항목 it의 행별 spec-out 불리언 마스크."""
        lo, hi = spec.get(it, (None, None))
        if (lo is None and hi is None) or it not in frame.columns:
            return None
        v = pd.to_numeric(frame[it], errors='coerce')
        m = pd.Series(False, index=frame.index)
        if lo is not None:
            m = m | (v < lo)
        if hi is not None:
            m = m | (v > hi)
        return m & v.notna()

    # PCHK 겹침 판정용: '다른(비-PCHK) 항목'의 타깃 lot shot별 spec-out 마스크
    other_masks = {}
    if pchk_aliases and col_x and col_y and col_waf:
        for _it in items:
            if _it in pchk_set or _it not in tgt.columns:
                continue
            _m = _outmask(tgt, _it)
            if _m is not None and int(_m.sum()) > 0:
                other_masks[_it] = _m

    def _pchk_overlap(it, allowed=None):
        """PCHK it의 spec-out shot에서 '동일 shot 동시 spec-out' 다른 항목을 집계.

        merged_df(=tgt)는 (wafer·tkout_time·step_seq·CHIP_X/Y …)로 피벗돼 **한 행 = 한 shot**
        이고, 그 행의 모든 item은 같은 touchdown = **동일 PGM(pt)**에서 측정된 값이다. 따라서
        같은 행 인덱스에서 함께 spec-out이면 자동으로 '동일 PGM(pt)·동일 CHIP_X/Y' 이다.
        (같은 chip이 2번 측정되면 tkout_time이 달라 다른 행=다른 PGM(pt) → 서로 안 섞임.)

        allowed: 이 PCHK의 '검증 대상 ITEM' alias 집합(None이면 모든 항목 대조).
        반환 (겹친 shot수, {item:겹친수}, 예시목록[(wafer,x,y,PGM(pt),[겹친item...])]).
        """
        pm = _outmask(tgt, it)
        if pm is None:
            return 0, {}, []
        ov_items, ov_shots, examples = {}, 0, []
        for idx in tgt.index[pm.values]:
            co = [k for k, m in other_masks.items()
                  if (allowed is None or k in allowed) and bool(m.get(idx, False))]
            if co:
                ov_shots += 1
                for k in co:
                    ov_items[k] = ov_items.get(k, 0) + 1
                if len(examples) < 5:
                    _w = _waf_int(tgt.at[idx, col_waf])
                    try:
                        _xx, _yy = int(tgt.at[idx, col_x]), int(tgt.at[idx, col_y])
                    except Exception:
                        _xx, _yy = tgt.at[idx, col_x], tgt.at[idx, col_y]
                    # 이 shot(=행)의 PGM(pt) — 겹친 항목 전부 이 값과 동일(같은 행이므로)
                    _pgm = (str(tgt.at[idx, col_pgm])
                            if col_pgm and col_pgm in tgt.columns else '')
                    examples.append((_w, _xx, _yy, _pgm, co))
        return ov_shots, ov_items, examples

    # ── trend_tkout_agg 항목: 이상/주의 판정을 'agg된 값' 기준으로 ──
    #   P10 등 지정 항목/이름에 'window' 포함 항목은 raw point 대신
    #   (mask,lot,root,wafer,match_key,tkout) 그룹별 agg 1값으로 치환 → spec-out·산포 판정이
    #   Trend와 동일한 집계값 기준으로 이뤄진다. (그룹 첫 행에 agg값, 나머지 NaN)
    _agg_item_set = set()
    try:
        import numpy as _np
        _agg_map = (getattr(config, 'trend_tkout_agg', {}) or {}) if config else {}

        def _agg_fn_for(_it):
            _spec = _agg_map.get(_it)
            if not _spec and 'window' in str(_it).lower():
                _spec = 'P10'
            if not _spec:
                return None
            _s = str(_spec).strip().upper()
            if _s in ('MEAN', 'AVG'):
                return 'mean'
            if _s in ('MEDIAN', 'P50'):
                return 'median'
            _m = re.match(r'P(\d+(?:\.\d+)?)$', _s)
            if _m:
                _q = min(max(float(_m.group(1)) / 100.0, 0.0), 1.0)
                return (lambda s, _qq=_q: s.quantile(_qq))
            return 'median'

        _agg_items = [it for it in items if _agg_fn_for(it) is not None]
        if _agg_items:
            _root_col = _pick_col(pop, 'ROOT_LOT_ID', 'root_lot_id')
            _gk = [c for c in [col_mask, col_lot, _root_col, col_waf,
                               ('match_key' if 'match_key' in pop.columns else None), col_time]
                   if c and c in pop.columns]
            if _gk:
                pop = pop.copy(); tgt = tgt.copy()
                for _ai in _agg_items:
                    _fn = _agg_fn_for(_ai)
                    for _fr in (pop, tgt):
                        if _ai not in _fr.columns or len(_fr) == 0:
                            continue
                        _num = pd.to_numeric(_fr[_ai], errors='coerce')
                        _tmp = _fr[_gk].copy(); _tmp['_v'] = _num.values
                        _bcast = _tmp.groupby(_gk)['_v'].transform(_fn)
                        _first = ~_fr.duplicated(subset=_gk)
                        _fr[_ai] = _np.where(_first.values, _bcast.values, _np.nan)
                    _agg_item_set.add(_ai)
    except Exception as _ae:
        print(f"[WARN] trend_tkout_agg 판정용 집계 실패: {_ae}")

    _basis = []      # 판단 근거 중간 데이터 (RUN/TEMP 저장용) — 전 Index 통합
    _rankinfo = {}   # 항목별 정렬 지표 (spec-out 비율/wafer 수/이탈 크기/REPORT ORDER)
    _item_ctx = {}   # 규칙 평가용 항목별 컨텍스트 {level, disp, tmed, pmed, pspread}
    for it in items:
        is_pchk = it in pchk_set
        lo, hi = spec.get(it, (None, None))
        pop_med, pop_spread = _robust(pop[it]) if it in pop.columns else (None, None)
        # 제품 전체를 wafer 단위로 본 기준(중심/wafer간 산포/보통 wafer 산포)
        w_center, w_scatter, typ_wspread = _pop_wafer_baseline(it)
        tgt_it = None
        if col_waf and col_lot and it in tgt.columns and len(tgt) > 0:
            tgt_it = tgt[[col_waf, it]].dropna(subset=[it])
            if len(tgt_it) == 0:
                tgt_it = None

        # ── 주의(WARNING) 신호: target lot의 '각 wafer'를 제품 wafer 기준과 비교 ──
        #   median 이탈 σ = |wafer median − 제품 wafer median 중심| / 제품 wafer median 산포(wafer간 변동)
        #   산포 배수     = wafer 내부 robust 산포 / 보통 wafer 산포
        #   항목 대표값 = target lot wafer 중 '가장 심한' wafer(worst).
        dev_txt, disp_txt = '', ''
        worst_med_dev, worst_med_w, worst_med_val = 0.0, None, None
        worst_disp_ratio, worst_disp_w = 0.0, None
        if tgt_it is not None:
            for w, g in tgt_it.groupby(col_waf):
                _s = pd.to_numeric(g[it], errors='coerce').dropna()
                if len(_s) == 0:
                    continue
                wm, ws = _robust(_s)
                if wm is not None and w_center is not None and w_scatter:
                    d = abs(wm - w_center) / w_scatter
                    if d > worst_med_dev:
                        _wi = _waf_int(w)
                        worst_med_dev = d
                        worst_med_w = _wi if _wi is not None else w
                        worst_med_val = wm
                if ws and typ_wspread:
                    r = ws / typ_wspread
                    if r > worst_disp_ratio:
                        _wi = _waf_int(w)
                        worst_disp_ratio = r
                        worst_disp_w = _wi if _wi is not None else w
            if worst_med_dev > 0 and worst_med_w is not None:
                dev_txt = f"#{worst_med_w} median {worst_med_dev:.1f}σ 이탈(제품 wafer 기준)"
            if worst_disp_ratio >= 1.3 and worst_disp_w is not None:
                disp_txt = f"#{worst_disp_w} 산포 {worst_disp_ratio:.1f}배"

        # spec-out을 wafer별 pt개수로 그룹 + 순위지표(최고 wafer 비율/spec-out wafer 수) + PGM(pt)/zone
        specout_txt, n_out, specout_map = ('', 0, {})
        so_max_ratio, so_n_wafers = 0.0, 0
        so_pgms, so_zones = [], {}
        if tgt_it is not None and (lo is not None or hi is not None):
            specout_txt, n_out, specout_map, so_max_ratio, so_n_wafers = \
                _specout_by_wafer(tgt_it, col_waf, it, lo, hi)
            if n_out > 0:
                so_pgms, so_zones = _specout_extra(it, lo, hi)
        # agg 판정 항목은 raw metrics 폴백을 쓰지 않는다(집계값 기준 유지)
        if n_out == 0 and it not in _agg_item_set:
            n_out = int(metrics_dict.get(it, {}).get('spec_out_count', 0) or 0)

        # PCHK 겹침(측정이상) 신호 — basis 기록용(AI가 측정이상 추정에 활용). finding/severity엔 미반영.
        #   PCHK 종류별 '검증 대상 ITEM'(매핑)으로 대조 범위를 한정한다.
        ov_shots, ov_items, ov_examples = (0, {}, [])
        _meas_target_tokens, _meas_target_resolved = (None, None)
        if is_pchk and n_out > 0:
            _device_items = [d for d in items if d not in pchk_set]
            _allowed, _meas_target_tokens = _resolve_allowed(it, _device_items)
            _meas_target_resolved = sorted(_allowed) if _allowed is not None else None
            ov_shots, ov_items, ov_examples = _pchk_overlap(it, _allowed)

        # 순위 지표 축적(정렬용) — 겹침신호는 순위에 미반영(AI 전용)
        _rankinfo[it] = {
            'max_ratio': float(so_max_ratio),
            'n_so_wafers': int(so_n_wafers),
            'worst_med_dev': float(worst_med_dev),
            'worst_disp_ratio': float(worst_disp_ratio),
            'report_order': report_order.get(it, 1e9),
        }

        # 상세: robust/제품-비교 등 공통 문구는 상단 '참고사항'에서 1회 안내 → 여기선 생략.
        #       PCHK 포함 모든 항목 동일하게 spec-out 위치(zone)/PGM(pt)·wafer 이탈을 덧붙인다.
        _bits = []
        if specout_txt:
            _bits.append(specout_txt)
        if so_zones:
            _bits.append('위치: ' + ', '.join(
                f"{z} {so_zones[z]}" for z in ('Center', 'Middle', 'Edge') if z in so_zones))
        if so_pgms:
            _bits.append('PGM(pt): ' + ', '.join(so_pgms))
        # median 이탈(dev_txt)은 판정 기준에서 제외됨 → 상세에도 표시하지 않음. 산포(disp_txt)만.
        if disp_txt:
            _bits.append(disp_txt)
        detail = '. '.join(_bits)

        # 판단 severity 결정 — 모든 Index 동일 기준(PCHK 특수처리 없음).
        #   이상(CRITICAL): spec(LCL/UCL) 이탈 point가 하나라도 있으면 이상. (median 기준 미사용)
        #   주의(WARNING) : spec 이내지만 '해당 wafer의 내부 산포'가 다른 wafer(보통 wafer) 대비 큰 경우.
        #   그 외         : 참고(INFO).
        if n_out > 0:
            _sev = 'CRITICAL'
        elif worst_disp_ratio > disp_ratio:
            _sev = 'WARNING'
        else:
            _sev = 'INFO'

        # ── 지식 규칙 평가용 항목 컨텍스트 (severity level / 산포배수 / target·pop median) ──
        _tmed = None
        if tgt_it is not None:
            try:
                _tmed = float(pd.to_numeric(tgt_it[it], errors='coerce').median())
            except Exception:
                _tmed = None
        _item_ctx[it] = {
            'level': 2 if _sev == 'CRITICAL' else (1 if _sev == 'WARNING' else 0),
            'disp': float(worst_disp_ratio) if worst_disp_ratio else 0.0,
            'tmed': _tmed, 'pmed': pop_med, 'pspread': pop_spread,
        }

        # ── 근거 데이터 축적 (전 Index 통합 스키마) ──
        #   meas_* 필드는 PCHK spec-out 항목에서만 채워지고 나머지는 기본값(0/{}/[]).
        _basis.append({
            'item': it, 'vehicle': _veh, 'target_lot': target_lot_id, 'severity': _sev,
            'is_pchk': is_pchk,
            'spec_low': lo, 'spec_high': hi,
            'spec_out_total': int(n_out),
            'spec_out_by_wafer': specout_map,       # {pt개수: [wafer, ...]}
            'spec_out_max_wafer_ratio': round(so_max_ratio, 4),   # 순위 1순위
            'spec_out_wafer_count': int(so_n_wafers),             # 순위 2순위
            'spec_out_pgm': so_pgms,
            'spec_out_zone': so_zones,              # {Center/Middle/Edge: 개수}
            'pop_median': pop_med, 'pop_robust_spread_MAD': pop_spread,   # 전체(chip) 참고
            'wafer_median_center': w_center,        # 제품 wafer median 중심
            'wafer_median_scatter': w_scatter,      # 제품 wafer median 산포(wafer간, median σ 분모)
            'typ_wafer_robust_spread': typ_wspread, # 보통 wafer 산포(산포배수 분모)
            'worst_median_wafer': worst_med_w,
            'worst_median_wafer_value': worst_med_val,
            'worst_median_dev_sigma': round(worst_med_dev, 3) if worst_med_dev else 0.0,
            'worst_dispersion_wafer': worst_disp_w,
            'worst_dispersion_ratio': round(worst_disp_ratio, 3) if worst_disp_ratio else 0.0,
            # 측정신뢰성(측정이상 추정, AI 전용) — 동일 shot 다른 항목 동시 spec-out 겹침
            'meas_target_items': _meas_target_tokens,       # 매핑에 적힌 검증 대상(원문 표기)
            'meas_target_resolved': _meas_target_resolved,  # 실제 매칭된 ITEM alias(None=전체)
            'meas_overlap_shot_count': int(ov_shots),
            'meas_overlap_items': ov_items,             # {item: 겹친 shot수}
            'meas_overlap_examples': [{'wafer': w, 'x': x, 'y': y, 'pgm': pgm, 'items': c}
                                      for (w, x, y, pgm, c) in ov_examples],
            'detail': detail.strip(),
        })

        # ── finding 산출 — 이상=spec-out only / 주의=wafer 산포 확대 only (median 판정 제거) ──
        if n_out > 0:
            findings.append(_finding(
                "CRITICAL", "SPEC_OUT", it,
                f"Spec-out: {_disp(it)}", detail.strip()))
            continue

        if worst_disp_ratio > disp_ratio:
            # 형식: "산포 확대 : ITEM - #W 산포 X배" (제목에 다 담고 상세는 비움 → 깔끔)
            findings.append(_finding(
                "WARNING", "DISPERSION", it,
                f"산포 확대 : {_disp(it)} - #{worst_disp_w} 산포 {worst_disp_ratio:.1f}배", ""))

    # ── 판단 근거 중간 데이터를 RUN/TEMP에 저장 (csv + json) ──
    try:
        import os, json
        _outdir = os.path.join('RUN', 'TEMP')
        os.makedirs(_outdir, exist_ok=True)
        _safe_lot = str(target_lot_id).replace('/', '_').replace('\\', '_')
        _base = os.path.join(_outdir, f"anomaly_basis_{_safe_lot}")
        with open(_base + '.json', 'w', encoding='utf-8') as _jf:
            json.dump(_basis, _jf, ensure_ascii=False, indent=2, default=str)
        # csv: dict/list 필드는 문자열로 평탄화
        _csv_rows = []
        for _b in _basis:
            _row = dict(_b)
            _row['spec_out_by_wafer'] = '; '.join(
                f"{k}pt:{','.join('#' + str(w) for w in v)}" for k, v in sorted(_b['spec_out_by_wafer'].items()))
            _row['meas_target_items'] = ', '.join(_b.get('meas_target_items') or [])
            _row['meas_target_resolved'] = ', '.join(_b.get('meas_target_resolved') or [])
            _row['meas_overlap_items'] = ', '.join(
                f"{k}({c})" for k, c in sorted(_b.get('meas_overlap_items', {}).items(),
                                               key=lambda z: -z[1]))
            _row['meas_overlap_examples'] = '; '.join(
                f"#{e['wafer']}({e['x']},{e['y']})@{e.get('pgm','')}→{'+'.join(e['items'])}"
                for e in _b.get('meas_overlap_examples', []))
            _csv_rows.append(_row)
        pd.DataFrame(_csv_rows).to_csv(_base + '.csv', index=False, encoding='utf-8-sig')
        print(f"[anomaly] 판단 근거 데이터 저장: {_base}.json / .csv ({len(_basis)}개 item)")
    except Exception as _be:
        print(f"[WARN] anomaly 근거 데이터 저장 실패: {_be}")

    # ── 지식 규칙(ANOMALY_KNOWLEDGE.md) 파싱·평가 → '지식 기반 판정' finding 생성 ──
    #   md의 RULE을 파싱해 항목별 통계 판정(_item_ctx: 이상/주의 level·산포배수·median)과 매칭.
    try:
        _kn_rules = _parse_knowledge_rules(knowledge_text)
        if _kn_rules:
            _mlow_sigma = cfg('anomaly_median_low_sigma', 2.0)

            _LV = {'이상': 2, '주의': 1, '참고': 0}

            def _find_ctx(name):
                _nf = _name_forms(name)
                for _cit, _cx in _item_ctx.items():
                    if _name_forms(_cit) & _nf:
                        return _cx
                return None

            def _resolve_item(name):
                """규칙의 항목명 → 실제 컨텍스트/finding 키(컬럼명). 없으면 None."""
                _nf = _name_forms(name)
                for _cit in _item_ctx:
                    if _name_forms(_cit) & _nf:
                        return _cit
                return None

            def _grp_disp(names):
                """그룹 항목들의 최대 산포배수(disp) 반환."""
                _vs = [(_find_ctx(n) or {}).get('disp', 0.0) for n in names]
                return max(_vs) if _vs else 0.0

            def _eval_atom(atom):
                atom = atom.strip()
                # sev(ITEM) 연산자: >= <= == < > , 등급 이상/주의/참고 (미측정 항목은 참고=0으로 간주)
                m = re.match(r'sev\(([^)]+)\)\s*(>=|<=|==|<|>)\s*(이상|주의|참고)$', atom)
                if m:
                    cx = _find_ctx(m.group(1).strip())
                    lv = cx['level'] if cx else 0
                    need = _LV[m.group(3)]; op = m.group(2)
                    return {'>=': lv >= need, '<=': lv <= need, '==': lv == need,
                            '<': lv < need, '>': lv > need}[op]
                m = re.match(r'all_sev\(([^)]+)\)\s*>=\s*(이상|주의)$', atom)
                if m:
                    need = 2 if m.group(2) == '이상' else 1
                    _ns = [t.strip() for t in m.group(1).split(',') if t.strip()]
                    _cs = [_find_ctx(n) for n in _ns]
                    return bool(_cs) and all(c and c['level'] >= need for c in _cs)
                m = re.match(r'disp_(desc|asc)\(([^)]+)\)$', atom)
                if m:
                    _ns = [t.strip() for t in m.group(2).split(',') if t.strip()]
                    _vs = []
                    for n in _ns:
                        c = _find_ctx(n)
                        if not c:
                            return False
                        _vs.append(c['disp'])
                    if len(_vs) < 2:
                        return False
                    if m.group(1) == 'desc':
                        return all(_vs[i] > _vs[i + 1] for i in range(len(_vs) - 1))
                    return all(_vs[i] < _vs[i + 1] for i in range(len(_vs) - 1))
                m = re.match(r'median_low\(([^)]+)\)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    if not c or c['tmed'] is None or c['pmed'] is None or not c['pspread']:
                        return False
                    return (c['pmed'] - c['tmed']) / c['pspread'] >= _mlow_sigma
                return False

            def _eval_when(expr):
                for _grp in re.split(r'\s+OR\s+', expr):
                    _atoms = [a for a in re.split(r'\s+AND\s+', _grp) if a.strip()]
                    if _atoms and all(_eval_atom(a) for a in _atoms):
                        return True
                return False

            _suppress_disp_items = set()   # DISPERSION(주의) finding을 억제할 항목(실제 키)
            for _r in _kn_rules:
                try:
                    _when = _r.get('when') or ''
                    _when_ok = (not _when) or _eval_when(_when)
                    # (1) 산포 언급 억제: WHEN 없으면 항상, 있으면 WHEN 참일 때만
                    if _r.get('suppress_disp'):
                        if _when_ok:
                            for _n in _r['suppress_disp']:
                                _ri = _resolve_item(_n)
                                if _ri:
                                    _suppress_disp_items.add(_ri)
                        continue
                    # (2) 산포 그룹 비교: WHEN 참일 때 두 그룹 산포 비교 코멘트 생성
                    if _r.get('compare_disp'):
                        if _when_ok:
                            _g1, _g2 = _r['compare_disp']
                            _v1, _v2 = _grp_disp(_g1), _grp_disp(_g2)
                            _big = f"[{', '.join(_g1)}]" if _v1 >= _v2 else f"[{', '.join(_g2)}]"
                            _cmp = f"산포 비교: [{', '.join(_g1)}]={_v1:.1f}배 vs [{', '.join(_g2)}]={_v2:.1f}배 → {_big} 산포가 더 큼"
                            _lvl = 'CRITICAL' if str(_r.get('level', '주의')).strip() == '이상' else 'WARNING'
                            _det = _cmp + ((' · ' + _r['note']) if _r.get('note') else '')
                            if _r.get('link'):
                                _det += f"  참고: {_r['link']}"
                            findings.append(_finding(_lvl, 'KNOWLEDGE', _r['label'],
                                                     f"[지식 판정] {_r['label']}", _det.strip()))
                        continue
                    # (3) 일반 판정: WHEN 참이면 finding 생성
                    if _when and _eval_when(_when):
                        _lvl = 'CRITICAL' if str(_r.get('level', '주의')).strip() == '이상' else 'WARNING'
                        _det = _r.get('note', '') or ''
                        if _r.get('link'):
                            _det = (_det + '  ' if _det else '') + f"참고: {_r['link']}"
                        findings.append(_finding(_lvl, 'KNOWLEDGE', _r['label'],
                                                 f"[지식 판정] {_r['label']}", _det.strip()))
                except Exception:
                    continue
            # 억제 적용: 지정 항목의 DISPERSION(주의) finding 제거 (spec-out=이상은 유지)
            if _suppress_disp_items:
                findings[:] = [f for f in findings
                               if not (f.get('type') == 'DISPERSION' and f.get('item') in _suppress_disp_items)]
    except Exception as _ke:
        print(f"[WARN] 지식 규칙 평가 실패: {_ke}")

    # ── Priority(우선순위) 명시적 수식 — 값이 클수록 우선(위에 정렬) ──
    #   이상(SPEC_OUT) : P = 20000 + 100·R_max + N_wf/100
    #        R_max = 항목 내 '최대 wafer spec-out 비율' = max_wafer(이탈 pt / 측정 pt), 0~1
    #        N_wf  = spec-out wafer 수 (0~25, 동점 tie-break용으로 /100 축소)
    #   주의(DISPERSION): P = 10000 + 100·D
    #        D = 항목 내 '최대 wafer 산포배수' = max_wafer(wafer 내부 robust 산포 / 보통 wafer 산포)
    #   참고(그 외)     : P = 100·D
    #   → 이상(20000+) > 주의(10000+) > 참고(<수백) 순이 항상 보장. 동점 시 REPORT ORDER 오름차순.
    def _priority(f):
        ri = _rankinfo.get(f.get('item', ''), {})
        t = f.get('type')
        if t == 'KNOWLEDGE':   # 지식 기반 판정(불량모드/risk 등)은 종합 결론 → 최상단
            return 30000.0 + (100.0 if f.get('severity') == 'CRITICAL' else 0.0)
        if t == 'SPEC_OUT':
            return 20000.0 + 100.0 * ri.get('max_ratio', 0.0) + ri.get('n_so_wafers', 0) / 100.0
        if t == 'DISPERSION':
            return 10000.0 + 100.0 * ri.get('worst_disp_ratio', 0.0)
        return 100.0 * ri.get('worst_disp_ratio', 0.0)

    for _f in findings:
        _f['priority'] = round(_priority(_f), 3)   # 투명성 위해 finding에 priority 값 부착

    findings.sort(key=lambda f: (-_priority(f),
                                 _rankinfo.get(f.get('item', ''), {}).get('report_order', 1e9)))
    return findings


def render_findings_html(findings, top_n=5, detail_ref="PPT의 Score Board 다음 'Anomaly 상세(통계)' 페이지"):
    """Finding 리스트를 HTML로 렌더링 (상위 top_n건만, 나머지는 PPT 상세 참조 안내).

    findings는 analyze_commonality에서 severity 순으로 정렬되어 들어온다.
    """
    if not findings:
        return ('<ul style="font-size:14px; color:#333; margin:5px 0 15px; padding-left:20px;">'
                '<li><strong>[요약]</strong> 통계 자동 분석 결과 유의미한 이상/commonality 신호 없음.</li></ul>')
    n_crit = sum(1 for f in findings if f["severity"] == "CRITICAL")
    n_warn = sum(1 for f in findings if f["severity"] == "WARNING")
    # head: 신호등 범례 겸 건수 (● 이상 N | ● 주의 X). 측정이상 추정은 코드 미판정(AI 전용).
    _div = ' <span style="color:#bbb;">|</span> '
    head = (f'<div style="font-size:13px; color:#333; margin:4px 0;">'
            f'<b>통계 기반 자동 분석</b>: '
            f'{_sev_dot("CRITICAL")} {_SEV_HEAD["CRITICAL"]} {n_crit}건{_div}'
            f'{_sev_dot("WARNING")} {_SEV_HEAD["WARNING"]} {n_warn}건</div>')
    shown = findings[:top_n]
    lis = []
    for f in shown:
        lis.append(
            f'<li style="margin-bottom:5px; list-style:none;">'
            f'{_sev_badge(f["severity"])} '
            f'<b>{f["title"]}</b>'
            + (f'<br><span style="color:#555; font-size:12px;">{f["detail"]}</span>' if f.get("detail") else "")
            + '</li>')
    more = ""
    if len(findings) > top_n:
        more = (f'<div style="font-size:12px; color:#555; margin:4px 0 0;">'
                f'… 우선순위 상위 {top_n}건만 표시. 전체 {len(findings)}건의 상세는 '
                f'<b>{detail_ref}</b>를 참조하세요.</div>')
    return head + ('<ul style="font-size:13px; color:#333; margin:5px 0 8px; padding-left:4px; list-style:none;">'
                   + "".join(lis) + '</ul>') + more


def interpret_with_ai(findings, metrics_dict, knowledge_text, llm_fn,
                      config=None, target_lot_id=""):
    """AI 다단계 해석: 각 단계의 판단을 다음 단계 입력으로 넘겨 최종 판단 생성.

    단계
    ----
    1) Triage   : 코드 Finding을 현상(phenomenon) 단위로 묶고 중요도 정렬(파생항목 중복 통합)
    2) RootCause: 지식베이스(knowledge_text)를 참고해 각 현상의 추정 원인 도출
    3) Final    : 1·2를 종합해 최종 판단/권고를 HTML로 산출

    Parameters
    ----------
    findings : list[dict]   analyze_commonality() 결과
    metrics_dict : dict     항목별 통계(참고용)
    knowledge_text : str     ANOMALY_KNOWLEDGE.md 내용(통계 패턴→원인)
    llm_fn : callable        llm_fn(system: str, user: str) -> str. 없으면 None 반환(코드만 사용)
    config, target_lot_id : 메타

    Returns
    -------
    str | None : [0] 섹션에 곁들일 AI 해석 HTML. 실패/미사용 시 None(→ 코드 분석만 표시)
    """
    if llm_fn is None or not findings:
        return None
    import json

    def _slim(f):
        return {k: f.get(k) for k in ("severity", "type", "item", "title", "detail")}
    fjson = json.dumps([_slim(f) for f in findings], ensure_ascii=False)
    # spec-out으로 분류된 Index 조합 (불량 모드 판정 입력)
    spec_items = [f.get('item') for f in findings if f.get('type') == 'SPEC_OUT']

    # ── AI 입력/출력 덤프 ──
    #   LLM에 실제로 보낸 system/user 프롬프트와 응답을 RUN/AI에 남긴다(삭제하지 않음 — 감사/재현용).
    #   (측정 raw/reformatter는 AI에 전달하지 않음 — findings 요약 + 지식베이스 텍스트만)
    _io = []

    def _stage(name, system, user):
        out = llm_fn(system, user)
        _io.append({"stage": name, "system": system, "user": user, "output": out})
        return out

    def _dump_ai_input():
        try:
            import os
            _outdir = os.path.join('RUN', 'AI')   # AI 인풋파일 보관 폴더(사이클 종료 후에도 유지)
            os.makedirs(_outdir, exist_ok=True)
            _safe = str(target_lot_id).replace('/', '_').replace('\\', '_') or 'lot'
            _base = os.path.join(_outdir, f"ai_input_{_safe}")
            with open(_base + '.json', 'w', encoding='utf-8') as _jf:
                json.dump({"target_lot_id": target_lot_id,
                           "findings": [_slim(f) for f in findings],
                           "stages": _io}, _jf, ensure_ascii=False, indent=2, default=str)
            _lines = [f"# AI 입력/출력 덤프 — lot {target_lot_id}", "",
                      "> `anomaly_engine.interpret_with_ai`가 LLM에 실제로 보낸 system/user 프롬프트와 응답.",
                      "> 측정 raw 데이터/reformatter는 전달하지 않고, 코드 findings 요약 + ANOMALY_KNOWLEDGE.md 텍스트만 넣는다.",
                      "", "## 입력 findings (JSON)", "", "```json", fjson, "```", ""]
            for _i, _s in enumerate(_io, 1):
                _lines += [f"## [{_i}] {_s['stage']}", "", "### system (프롬프트 + 지식베이스)",
                           "```", str(_s['system']), "```", "",
                           "### user (직전 단계 산출/데이터)", "```", str(_s['user']), "```", "",
                           "### output (LLM 응답)", "```", str(_s['output']), "```", ""]
            with open(_base + '.md', 'w', encoding='utf-8') as _mf:
                _mf.write("\n".join(_lines))
            print(f"[anomaly] AI 입력 덤프 저장: {_base}.md / .json ({len(_io)} stage)")
        except Exception as _de:
            print(f"[WARN] AI 입력 덤프 저장 실패: {_de}")

    try:
        # ── 1단계: Triage / 현상 그룹핑 ──
        triage = _stage(
            "① Triage",
            "당신은 반도체 TEG 데이터 분석가입니다. 코드가 산출한 이상 finding 목록을 "
            "현상(phenomenon) 단위로 묶고 중요도 순으로 3~6개로 정리하세요. "
            "파생항목(예: IDSAT_N/RATIO/SUM)이 같은 현상이면 하나로 통합하고, "
            "PCHK(측정 의심)가 있으면 최상단에 별도 표기하세요. 간결한 불릿으로. "
            "**주어진 finding에 적힌 사실(항목·spec-out·이탈 수치)만 사용**하고, 원인·해석·"
            "반도체 공정 지식을 추가하지 마세요(요약/그룹핑만).",
            f"대상 lot: {target_lot_id}\n[findings]\n{fjson}")

        # ── 2단계: Root-cause (지식베이스 참고) ──
        rootcause = _stage(
            "② Root-cause",
            "당신은 수율/공정 엔지니어입니다. 아래 [지식베이스]에 **명시적으로 적힌 내용만** "
            "근거로 각 현상을 연결하세요. **[지식베이스]에 적혀있지 않은 원인·반도체 공정 지식"
            "(예: '산화막 두께', '식각 균일성' 등)은 절대 추측하거나 지어내지 마세요.** "
            "[지식베이스]에 해당 근거가 없으면 그 현상은 반드시 '지식베이스 미기재 — 추가 분석 필요'"
            "라고만 적고, 임의의 원인을 붙이지 마세요. 데이터(finding)에서 관찰된 사실과 "
            "지식베이스에 적힌 문장 외에는 기술하지 마세요.\n"
            f"[지식베이스]\n{knowledge_text or '(지식베이스 없음)'}",
            f"[현상 정리]\n{triage}")

        # ── 3단계: 최종 판단 + 불량 모드 판정 ──
        #   spec-out Index 조합을 [지식베이스]의 '불량 모드 판정표'와 대조하여 판정.
        #   여러 모드가 동시 매칭되면 표에서 더 위(번호 작은) 모드를 택한다.
        final = _stage(
            "③ Final",
            "당신은 책임 엔지니어입니다. 아래 현상/근거를 종합해 최종 판단을 내리되, "
            "**[지식베이스]와 관찰된 데이터(finding)에 있는 내용만** 사용하세요. "
            "**[지식베이스]에 적혀있지 않은 반도체 공정 지식·원인·조치(예: '산화막 두께', "
            "'식각 균일성' 등 md에 없는 도메인 지식)를 임의로 판단하거나 추가하지 마세요.** "
            "[지식베이스]의 '불량 모드 판정표'를 이용해 spec-out Index 조합으로부터 불량 모드를 "
            "판정하되, 표는 위에서부터 우선순위가 높고 여러 모드가 동시 매칭되면 **번호가 가장 작은"
            "(가장 위)** 모드 하나로 판정합니다(1-1, 1-2 세부도 위가 우선). "
            "매칭이 없으면 '특정 불량 모드 미매칭(수동 검토)'으로 적으세요. "
            "측정 의심(PCHK 동일 shot 겹침)이 있으면 [지식베이스]의 '측정이상 추정 규칙'을 적용해 "
            "불량 단정 전 재측정 권고를 우선하세요. "
            "반드시 HTML <ul>로만 출력: "
            "<li><b>[불량 모드 판정]</b> (판정표에 있는 모드명) — (근거가 된 Index 조합)</li>"
            "<li><b>[종합 판단]</b> (finding·지식베이스에 근거한 판단만)</li>"
            "<li><b>[핵심 현상]</b> (데이터에서 **관찰된 사실만** — 어느 Index가 어떻게 spec-out/이탈했는지. 원인 추측 금지)</li>"
            "<li><b>[권고 조치]</b> ([지식베이스]에 명시된 조치만. 없으면 '지식베이스 미기재')</li>.\n"
            f"[지식베이스]\n{knowledge_text or '(지식베이스 없음)'}",
            f"[spec-out Index 조합]\n{', '.join(spec_items) if spec_items else '(없음)'}\n\n"
            f"[현상]\n{triage}\n\n[근거]\n{rootcause}")

        body = final if "<" in str(final) else f"<ul><li>{final}</li></ul>"
        note = ('<div style="font-size:11px; color:#9aa0a6; font-style:italic; margin:2px 0 4px;">'
                '※ 아래 내용은 AI가 자동 생성한 참고용 요약입니다. 보조 자료로만 활용하세요.</div>')
        # 본문은 검정 글씨 + 글머리 점 제거(list-style:none)
        return note + (
            '<div class="ai-interp" style="color:#1a1a1a; font-size:13px;">'
            '<style>.ai-interp ul{list-style:none; padding-left:0; margin:4px 0;} '
            '.ai-interp li{margin-bottom:4px;}</style>'
            f'{body}</div>')
    except Exception as e:
        print(f"[anomaly] AI 다단계 해석 실패(코드 분석으로 대체): {e}")
        return None
    finally:
        # 성공/실패와 무관하게 지금까지 조립된 AI 입력/출력을 남긴다.
        _dump_ai_input()
