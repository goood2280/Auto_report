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


def _finding(sev, ftype, item, title, detail=""):
    return {"severity": sev, "type": ftype, "item": item, "title": title, "detail": detail}


def analyze_commonality(merged_df, target_lot_id, metrics_dict, spec_data,
                        main_vehicle=None, config=None, reformatter=None):
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
        """target lot을 wafer별 spec-out pt 개수로 그룹핑 → (텍스트, 총개수, {pt개수: [wafer,...]})."""
        _v = pd.to_numeric(tgt_it[it], errors='coerce')
        _om = pd.Series(False, index=tgt_it.index)
        if lo is not None: _om = _om | (_v < lo)
        if hi is not None: _om = _om | (_v > hi)
        n_total = int(_om.sum())
        if n_total == 0:
            return '', 0, {}
        by_cnt = defaultdict(list)
        for w, cnt in tgt_it[_om.values].groupby(col).size().items():
            wi = _waf_int(w)
            by_cnt[int(cnt)].append(wi if wi is not None else w)
        txt = ' / '.join(
            f"{c}pt out: {', '.join('#' + str(w) for w in sorted(by_cnt[c], key=lambda z: (z is None, z)))}"
            for c in sorted(by_cnt))
        return txt, n_total, {int(c): sorted(by_cnt[c], key=lambda z: (z is None, z)) for c in by_cnt}

    # ---- 각 Index 항목별 '한 개'의 이상 finding ----
    #   유형(우선순위): spec-out(CRITICAL) > median 이탈 > 산포 확대 (WARNING).
    #   상세는 median/모집단 median 수치 대신: wafer별 spec-out pt, worst wafer의 median 이탈(robust σ),
    #   lot 산포가 모집단 대비 몇 배인지(robust)로 서술. 비교는 해당 vehicle 내로 한정(with_vehicle 제외).
    _veh = main_vehicle or '모집단'

    def _typ_lot_spread(it):
        """'보통의 랏 산포': 제품(pop) 내 각 lot의 lot내 robust 산포의 중앙값."""
        if not col_lot or it not in pop.columns:
            return None
        _sp = []
        for _lid, _g in pop[[col_lot, it]].dropna(subset=[it]).groupby(col_lot):
            _, _s = _robust(_g[it])
            if _s:
                _sp.append(_s)
        return float(pd.Series(_sp).median()) if _sp else None

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
        pgms = ([str(p) for p in _so[col_pgm].dropna().unique()]
                if col_pgm and col_pgm in _so.columns else [])
        zones = {}
        if col_x and col_y and col_x in _so.columns and col_y in _so.columns:
            _r = np.sqrt(pd.to_numeric(_so[col_x], errors='coerce') ** 2 +
                         pd.to_numeric(_so[col_y], errors='coerce') ** 2)
            for z in _r.dropna().map(_zone_of):
                zones[z] = zones.get(z, 0) + 1
        return pgms, zones

    _basis = []   # 판단 근거 중간 데이터 (RUN/TEMP 저장용)
    for it in items:
        lo, hi = spec.get(it, (None, None))
        pop_med, pop_spread = _robust(pop[it]) if it in pop.columns else (None, None)
        typ_spread = _typ_lot_spread(it)   # 보통의 랏 산포
        tgt_it = None
        if col_waf and col_lot and it in tgt.columns and len(tgt) > 0:
            tgt_it = tgt[[col_waf, it]].dropna(subset=[it])
            if len(tgt_it) == 0:
                tgt_it = None

        # wafer별 median 이탈 (제품 median/제품 전체 산포 기준) — worst wafer
        dev_txt = ''
        lot_dev = 0.0
        worst_w, worst_dev, lot_med = None, None, None
        if tgt_it is not None and pop_med is not None and pop_spread:
            _num = pd.to_numeric(tgt_it[it], errors='coerce')
            wmed = _num.groupby(tgt_it[col_waf]).median().dropna()
            if len(wmed) > 0:
                dev_by_w = ((wmed - pop_med) / pop_spread).abs()
                _ww = dev_by_w.idxmax(); _wd = float(dev_by_w.max())
                _wwi = _waf_int(_ww)
                worst_w, worst_dev = (_wwi if _wwi is not None else _ww), _wd
                dev_txt = f"wafer #{worst_w} median {_wd:.1f}σ 이탈"
            lot_med = float(_num.median())
            lot_dev = abs(lot_med - pop_med) / pop_spread

        # lot내 산포가 '보통의 랏 산포' 대비 몇 배 — 큰 경우만 표기(양호=작은 경우는 미표기)
        disp_txt = ''
        disp_ratio_r = 0.0
        tgt_spread = None
        if tgt_it is not None and typ_spread:
            _, tgt_spread = _robust(tgt_it[it])
            if tgt_spread:
                disp_ratio_r = tgt_spread / typ_spread
                if disp_ratio_r >= 1.3:
                    disp_txt = f"lot 산포 {disp_ratio_r:.1f}배"

        # spec-out을 wafer별 pt개수로 그룹 + PGM(pt)/radius zone
        specout_txt, n_out, specout_map = ('', 0, {})
        so_pgms, so_zones = [], {}
        if tgt_it is not None and (lo is not None or hi is not None):
            specout_txt, n_out, specout_map = _specout_by_wafer(tgt_it, col_waf, it, lo, hi)
            if n_out > 0:
                so_pgms, so_zones = _specout_extra(it, lo, hi)
        if n_out == 0:
            n_out = int(metrics_dict.get(it, {}).get('spec_out_count', 0) or 0)

        # 상세: robust/제품-비교 등 공통 문구는 상단 '참고사항'에서 1회 안내 → 여기선 생략.
        #       spec-out이면 위치(zone)/PGM(pt)를 덧붙인다.
        _bits = []
        if specout_txt:
            _bits.append(specout_txt)
        if so_zones:
            _bits.append('위치: ' + ', '.join(
                f"{z} {so_zones[z]}" for z in ('Center', 'Middle', 'Edge') if z in so_zones))
        if so_pgms:
            _bits.append('PGM(pt): ' + ', '.join(so_pgms))
        if dev_txt:
            _bits.append(dev_txt)
        if disp_txt:
            _bits.append(disp_txt)
        detail = '. '.join(_bits)

        # 판단 severity 결정 (spec-out=이상 / median 이탈·산포 확대=주의)
        if n_out > 0:
            _sev = 'CRITICAL'
        elif lot_dev > sigma_med:
            _sev = 'WARNING'
        elif disp_ratio_r > disp_ratio:
            _sev = 'WARNING'
        else:
            _sev = 'INFO'

        # ── 근거 데이터 축적 (spec-out wafer/PGM/zone, robust 산포, 이탈도 등) ──
        _basis.append({
            'item': it, 'vehicle': _veh, 'target_lot': target_lot_id, 'severity': _sev,
            'spec_low': lo, 'spec_high': hi,
            'spec_out_total': int(n_out),
            'spec_out_by_wafer': specout_map,   # {pt개수: [wafer, ...]}
            'spec_out_pgm': so_pgms,
            'spec_out_zone': so_zones,          # {Center/Middle/Edge: 개수}
            'pop_median': pop_med, 'pop_robust_spread_MAD': pop_spread,
            'typ_lot_robust_spread': typ_spread,
            'target_median': lot_med, 'target_robust_spread_MAD': tgt_spread,
            'lot_median_dev_sigma': round(lot_dev, 3) if lot_dev else 0.0,
            'worst_wafer': worst_w,
            'worst_wafer_dev_sigma': round(worst_dev, 3) if worst_dev is not None else None,
            'dispersion_ratio_vs_typ_lot': round(disp_ratio_r, 3) if disp_ratio_r else 0.0,
            'detail': detail.strip(),
        })

        if n_out > 0:
            findings.append(_finding(
                "CRITICAL", "SPEC_OUT", it,
                f"Spec-out: {it} ({n_out}개 이탈)", detail.strip()))
            continue

        if lot_dev > sigma_med:
            findings.append(_finding(
                "WARNING", "MEDIAN_SHIFT", it,
                f"Median 이탈(spec 내): {it} {lot_dev:.1f}σ",
                detail.strip() or f"lot median {lot_dev:.1f}σ 이탈."))
        elif disp_ratio_r > disp_ratio:
            findings.append(_finding(
                "WARNING", "DISPERSION", it,
                f"산포 확대: {it} 보통 랏 대비 {disp_ratio_r:.1f}배",
                detail.strip() or "lot 산포 증가(균일도 검토)."))

    # ── 판단 근거 중간 데이터를 RUN/TEMP에 저장 (csv + json) ──
    try:
        import os, json
        _outdir = os.path.join('RUN', 'TEMP')
        os.makedirs(_outdir, exist_ok=True)
        _safe_lot = str(target_lot_id).replace('/', '_').replace('\\', '_')
        _base = os.path.join(_outdir, f"anomaly_basis_{_safe_lot}")
        with open(_base + '.json', 'w', encoding='utf-8') as _jf:
            json.dump(_basis, _jf, ensure_ascii=False, indent=2, default=str)
        # csv: spec_out_by_wafer는 문자열로 평탄화
        _csv_rows = []
        for _b in _basis:
            _row = dict(_b)
            _row['spec_out_by_wafer'] = '; '.join(
                f"{k}pt:{','.join('#' + str(w) for w in v)}" for k, v in sorted(_b['spec_out_by_wafer'].items()))
            _csv_rows.append(_row)
        pd.DataFrame(_csv_rows).to_csv(_base + '.csv', index=False, encoding='utf-8-sig')
        print(f"[anomaly] 판단 근거 데이터 저장: {_base}.json / .csv ({len(_basis)}개 item)")
    except Exception as _be:
        print(f"[WARN] anomaly 근거 데이터 저장 실패: {_be}")

    def _rank(f):
        m = metrics_dict.get(f.get('item', ''), {})
        sev = _SEV_ORDER.get(f["severity"], 9)
        so = float(m.get('spec_out_count', 0) or 0)
        dev = float(m.get('deviation', 0.0) or 0.0)
        return (sev, -(so * 1000.0 + dev))

    findings.sort(key=_rank)
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
    n_note = sum(1 for f in findings if f["severity"] == "NOTICE")
    # head: 신호등 3색 범례 겸 건수 (● 이상 N | ● 주의 X | ● 측정이상 추정 Y)
    _div = ' <span style="color:#bbb;">|</span> '
    head = (f'<div style="font-size:13px; color:#333; margin:4px 0;">'
            f'<b>통계 기반 자동 분석</b>: '
            f'{_sev_dot("CRITICAL")} {_SEV_HEAD["CRITICAL"]} {n_crit}건{_div}'
            f'{_sev_dot("WARNING")} {_SEV_HEAD["WARNING"]} {n_warn}건{_div}'
            f'{_sev_dot("NOTICE")} {_SEV_HEAD["NOTICE"]} {n_note}건</div>')
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

    try:
        # ── 1단계: Triage / 현상 그룹핑 ──
        triage = llm_fn(
            "당신은 반도체 TEG 데이터 분석가입니다. 코드가 산출한 이상 finding 목록을 "
            "현상(phenomenon) 단위로 묶고 중요도 순으로 3~6개로 정리하세요. "
            "파생항목(예: IDSAT_N/RATIO/SUM)이 같은 현상이면 하나로 통합하고, "
            "PCHK(측정 의심)가 있으면 최상단에 별도 표기하세요. 간결한 불릿으로.",
            f"대상 lot: {target_lot_id}\n[findings]\n{fjson}")

        # ── 2단계: Root-cause (지식베이스 참고) ──
        rootcause = llm_fn(
            "당신은 수율/공정 엔지니어입니다. 아래 [지식베이스]를 근거로 각 현상의 "
            "추정 원인(이런 통계 차이가 무엇 때문인지)과 확인 포인트를 1~2줄로 제시하세요. "
            "지식베이스에 없으면 '추가 분석 필요'로 표기하세요.\n"
            f"[지식베이스]\n{knowledge_text or '(지식베이스 없음)'}",
            f"[현상 정리]\n{triage}")

        # ── 3단계: 최종 판단 + 불량 모드 판정 ──
        #   spec-out Index 조합을 [지식베이스]의 '불량 모드 판정표'와 대조하여 판정.
        #   여러 모드가 동시 매칭되면 표에서 더 위(번호 작은) 모드를 택한다.
        final = llm_fn(
            "당신은 책임 엔지니어입니다. 아래 현상/추정원인을 종합해 최종 판단을 내리세요. "
            "특히 [지식베이스]의 '불량 모드 판정표'를 이용해, spec-out으로 분류된 Index 조합으로부터 "
            "불량 모드를 판정하세요. 표는 위에서부터 우선순위가 높고, 여러 모드가 동시에 매칭되면 "
            "**번호가 가장 작은(가장 위)** 모드 하나로 판정합니다(1-1, 1-2 세부도 위가 우선). "
            "매칭이 없으면 '특정 불량 모드 미매칭(수동 검토)'으로 적으세요. "
            "반드시 HTML <ul>로만 출력: "
            "<li><b>[불량 모드 판정]</b> (판정 모드명) — (근거가 된 Index 조합)</li>"
            "<li><b>[종합 판단]</b> ...</li>"
            "<li><b>[핵심 현상·추정 원인]</b> ...</li>"
            "<li><b>[권고 조치]</b> ...</li>. 측정 의심이 있으면 불량 단정 전에 재측정 권고를 포함.\n"
            f"[지식베이스]\n{knowledge_text or '(지식베이스 없음)'}",
            f"[spec-out Index 조합]\n{', '.join(spec_items) if spec_items else '(없음)'}\n\n"
            f"[현상]\n{triage}\n\n[추정 원인]\n{rootcause}")

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
