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


def trend_agg_spec(name, agg_map, spec_name=None):
    """trend_tkout_agg 매칭 → 집계 스펙 문자열('P10'/'P90'/'MEDIAN'/'MEAN') 또는 None.

    My_config.trend_tkout_agg = {키: 스펙}. 키는 base ALIAS(예: 'MAWIN')로 두어도 그 파생
    컬럼(MAWIN_minus_margin, MAWIN_ovl_index …)까지 매칭되도록 아래 규칙으로 판정한다(대소문자 무시):
      (1) 항목명/spec명이 키와 정확히 일치
      (2) fnmatch 와일드카드(키에 * ? 사용, 예 'MAWIN_*')
      (3) prefix — 항목명이 '키' 또는 '키_'로 시작(파생 컬럼 포함)
      (4) 항목명/spec명에 'window' 포함 → 기본 'P10'(MA_Window 파생 자동 집계)
    anomaly_engine(이상/주의 판정)과 My_Function(Trend 차트)이 같은 규칙을 쓰도록 공용 함수.
    """
    import fnmatch
    names = [str(n) for n in (name, spec_name) if n not in (None, '')]
    for key, spec in (agg_map or {}).items():
        kl = str(key).lower()
        for s in names:
            sl = s.lower()
            if sl == kl or fnmatch.fnmatch(sl, kl) or sl.startswith(kl + '_'):
                return spec
    for s in names:
        if 'window' in s.lower():
            return 'P10'
    return None


def _finding(sev, ftype, item, title, detail="", **extra):
    """Finding dict 생성. extra(display_name·cat2·spec_out_* 등)는 AI 해석 입력용 부가정보 —
    HTML/PPT 렌더러는 severity/title/detail만 읽으므로 키 추가에 안전하다."""
    d = {"severity": sev, "type": ftype, "item": item, "title": title, "detail": detail}
    d.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return d


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


# ──────────────────────────────────────────────────────────────────────
# spec-out 공간 패턴(특이맵) 분류 — 제품/좌표계 무관, '규칙 목록' 기반
#   - 판정 규칙은 **오직 My_config.anomaly_pattern_rules(list)** 로만 정의한다(하드코딩 기본 규칙 없음).
#   - anomaly_pattern_rules 가 None/빈 리스트면 **특이맵(공간 패턴) 판정을 아예 하지 않는다**
#     (spec_out_pattern 라벨 미생성). 전역 옵션은 anomaly_pattern_thresholds(dict).
#   - 어떤 규칙이 어떤 값으로 평가·통과했는지는 stats['rules'] trace로 남는다
#     (anomaly_basis_<lot>.json의 spec_out_pattern_stats — "왜 이 특이맵인지" 근거).
#   - 판정식 상세·규칙 type별 파라미터는 README '특이맵(공간 패턴) 판정 기준' 참조.
# ──────────────────────────────────────────────────────────────────────
_PATTERN_OPT_DEFAULT = {
    'min_pts': 3,           # 패턴 판정 최소 unique 좌표 수(미만이면 '소수 pt' 보류)
    'y_positive_up': True,  # 좌표 y+가 웨이퍼 위(12시) 방향인지(반대면 False → 상/하 반전)
}


def classify_specout_pattern(out_xy, all_xy, radius_of=None, rules=None, options=None):
    """spec-out chip 좌표 집합의 공간 패턴(특이맵)을 분류한다 — 제품/좌표계 무관.

    제품별 chip 좌표 범위가 달라도 동작하도록 모든 판정을 정규화 좌표로 수행:
      - 중심(cx,cy) = 제품 전체 chip 좌표(all_xy)의 평균(centroid)
      - r_norm      = 좌표별 radius / 제품 최대 radius. radius는 radius_of
                      (설정파일 Chip_Radius 매핑, Data Extractor) 우선,
                      없으면 centroid 유클리드 거리로 대체
      - 방향        = centroid 기준 시계 각도(12시=위, 3시=오른쪽)

    rules(목록, 위에서부터 '먼저 통과'한 라벨 채택 — **None/빈 리스트면 판정하지 않음**):
      type='global'      : min_share — unique out 좌표/제품 전체 좌표 ≥ → 전면성
      type='line'        : axis('x'|'y'), max_lanes, min_pts — 서로 다른 축값 개수 ≤ → 줄성
      type='radius_band' : r_min, r_max, cover — r_norm∈[r_min,r_max) 비율 ≥ cover → 환형/링/센터
      type='clock'       : min_rnorm, resultant, min_frac — 방향 집중도 R ≥ → k시 방향
      type='quadrant'    : cover — 한 사분면(우상/좌상/좌하/우하) 비율 ≥
      type='half'        : cover — 한 반면(상/하/좌/우) 비율 ≥
    options: _PATTERN_OPT_DEFAULT(min_pts, y_positive_up) override.

    unique 좌표 수 < min_pts면 '소수 pt'(판정 보류). 좌표는 wafer간 중복을 제거해
    'lot 전체에서 그 위치가 이상인가'로 본다.

    반환 (label, stats):
      label = 패턴명(비율/방향 포함). 아무 규칙도 통과 못 하면 '산발(특정 패턴 없음)'.
      stats = 판정 근거 — 'rules'에 **모든 규칙의 평가값·통과여부 trace**가 남아
              "이 맵이 왜 이 특이맵으로 분류됐는지"를 basis에서 확인할 수 있다.
    """
    import math
    # 규칙(My_config.anomaly_pattern_rules)이 없으면 특이맵 판정을 하지 않음(하드코딩 기본규칙 없음)
    if not rules:
        return '', {'skipped': '패턴 규칙 미설정(My_config.anomaly_pattern_rules None/빈 리스트)'}
    opt = dict(_PATTERN_OPT_DEFAULT)
    opt.update(options or {})
    rule_list = rules
    try:
        pts = sorted({(float(x), float(y)) for x, y in out_xy})
        allp = [(float(x), float(y)) for x, y in all_xy]
    except (TypeError, ValueError):
        return '', {}
    if not pts or not allp:
        return '', {}
    cx = sum(p[0] for p in allp) / len(allp)
    cy = sum(p[1] for p in allp) / len(allp)
    _ysign = 1.0 if opt.get('y_positive_up', True) else -1.0

    def _rad(p):
        if radius_of:
            r = radius_of.get((p[0], p[1]))
            if r is not None:
                return float(r)
        return math.hypot(p[0] - cx, p[1] - cy)

    rmax = max(_rad(p) for p in allp) or 1.0
    rn = [_rad(p) / rmax for p in pts]
    n = len(pts)
    stats = {'n_out_coords': n, 'n_all_coords': len(allp),
             'out_coord_share': round(n / len(allp), 3), 'rules': []}
    if n < int(opt.get('min_pts', 3)):
        return f'소수 pt({n}개 좌표)', stats

    def _fmt_vals(vals):
        return ', '.join(str(int(v)) if float(v).is_integer() else f'{v:g}'
                         for v in sorted(vals))

    # 공용 파생값(사분면/반구/방향)
    q = {'우상': 0, '좌상': 0, '좌하': 0, '우하': 0}
    for p in pts:
        dx, dy = p[0] - cx, _ysign * (p[1] - cy)
        q['우상' if dx >= 0 and dy >= 0 else
          '좌상' if dx < 0 and dy >= 0 else
          '좌하' if dx < 0 else '우하'] += 1
    h = {'상': (q['우상'] + q['좌상']) / n, '하': (q['좌하'] + q['우하']) / n,
         '우': (q['우상'] + q['우하']) / n, '좌': (q['좌상'] + q['좌하']) / n}

    label = ''
    for rule in rule_list:
        t = str(rule.get('type', '')).lower()
        nm = rule.get('name', t)
        passed, metric, lab = False, None, ''
        try:
            if t == 'global':
                metric = stats['out_coord_share']
                passed = metric >= float(rule.get('min_share', 0.5))
                lab = f"{nm}(전 좌표의 {metric:.0%})"
            elif t == 'line':
                axis = str(rule.get('axis', 'x')).lower()
                vals = {p[0] for p in pts} if axis == 'x' else {p[1] for p in pts}
                metric = len(vals)
                passed = (n >= int(rule.get('min_pts', 4))
                          and metric <= int(rule.get('max_lanes', 2)))
                lab = f"{nm}({axis}={_fmt_vals(vals)})"
            elif t == 'radius_band':
                r_lo = float(rule.get('r_min', 0.0))
                r_hi = float(rule.get('r_max', 1.01))
                metric = round(sum(1 for r in rn if r_lo <= r < r_hi) / n, 3)
                passed = metric >= float(rule.get('cover', 0.7))
                lab = f"{nm}({metric:.0%})"
            elif t == 'clock':
                dirs = []
                for p, r in zip(pts, rn):
                    if r < float(rule.get('min_rnorm', 0.4)):
                        continue
                    dx, dy = p[0] - cx, _ysign * (p[1] - cy)
                    d = math.hypot(dx, dy)
                    if d > 0:
                        dirs.append((dx / d, dy / d))
                if len(dirs) >= int(opt.get('min_pts', 3)) \
                        and len(dirs) / n >= float(rule.get('min_frac', 0.75)):
                    ux = sum(d[0] for d in dirs) / len(dirs)
                    uy = sum(d[1] for d in dirs) / len(dirs)
                    metric = round(math.hypot(ux, uy), 3)   # 방향 집중도 R
                    passed = metric >= float(rule.get('resultant', 0.92))
                    if passed:
                        ang = math.degrees(math.atan2(ux, uy)) % 360   # 12시=0°, 시계방향
                        hour = int(round(ang / 30.0)) % 12 or 12
                        stats['clock_hour'] = hour
                        lab = (nm.replace('k시', f'{hour}시') if 'k시' in nm
                               else f'{hour}시 {nm}') + f'(집중도 {metric:.2f})'
            elif t == 'quadrant':
                bk = max(q, key=lambda k: q[k])
                metric = {'best': bk, 'frac': round(q[bk] / n, 2),
                          'all': {k: round(v / n, 2) for k, v in q.items()}}
                passed = q[bk] / n >= float(rule.get('cover', 0.7))
                lab = f"{bk} 사분면({q[bk] / n:.0%})"
            elif t == 'half':
                bk = max(h, key=lambda k: h[k])
                metric = {'best': bk, 'frac': round(h[bk], 2),
                          'all': {k: round(v, 2) for k, v in h.items()}}
                passed = h[bk] >= float(rule.get('cover', 0.75))
                lab = (f'{bk}반구({h[bk]:.0%})' if bk in ('상', '하')
                       else f'{bk}측 반면({h[bk]:.0%})')
            else:
                metric = f'알 수 없는 type: {t}'
        except Exception as _ce:
            metric = f'평가 실패: {_ce}'
        stats['rules'].append({'name': nm, 'type': t, 'metric': metric, 'passed': bool(passed)})
        if passed and not label:
            label = lab
    return (label or '산발(특정 패턴 없음)'), stats


def _parse_defect_modes(text):
    """AI Final의 불량 모드 판정 검증용 목록을 '통합 [RULE] 규칙'(ANOMALY_RULES 마커)에서 도출.

    ▶ 통합 관리: 판정 규칙은 [RULE] 체이닝 포맷 하나로만 관리한다.
      각 [RULE] 분기의 note가 곧 불량 모드명, 그 분기의 link가 대시보드 링크.
      규칙/분기 '순서' = 우선순위. 산포 억제/비교 전용 규칙(분기 note 없음)은 제외된다.
    반환: [{'num','mode','when','comment','link'}, ...] (규칙·분기 순서 = 우선순위).
    AI Final(JSON) 검증 — defect_mode/LINK가 이 목록에 있는 값인지 대조한다.
    """
    out = []
    if not text:
        return out
    _n = 0
    for _r in _parse_chain_rules(text):
        for _br in _r.get('branches', []):
            _note = (_br.get('note') or '').strip()
            if not _note:
                continue
            _n += 1
            _when = _r.get('when', '')
            if _br.get('cond'):
                _when = (_when + ' → ' if _when else '') + _br['cond']
            out.append({'num': str(_n), 'mode': _note, 'when': _when,
                        'comment': '', 'link': _br.get('link', '')})
    return out


def _extract_json_obj(text):
    """LLM 응답에서 JSON 객체를 추출(코드펜스/설명문 혼입 허용). 실패 시 None."""
    import json
    t = str(text or '').strip()
    i, j = t.find('{'), t.rfind('}')
    if i == -1 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _assemble_final_html(final_text, modes):
    """AI Final 단계의 구조화(JSON) 출력을 검증하고 **서술형 문장(평문 + 핵심 볼드)** 으로 조립.

    출력 형태(머리말 태그 없이 자연스러운 2~4문장):
      "**<불량 모드>**(으)로 추정됩니다(근거: **A, B**). <현상/원인 서술>.
       측정이상 가능성: <...> — 재측정 우선. 확인/조치: <...> [관련 링크]"

    검증 규칙(할루시네이션 차단):
    - defect_mode는 **ANOMALY_KNOWLEDGE.md [RULE]에 정의된 모드만** 인정 — 규칙에 없는 모드명은
      표기하지 않고 '지식 규칙 미매칭(수동 검토 필요)'로만 안내한다(AI 자유 제안 미표기).
    - LINK는 LLM 출력이 아니라 **매칭된 [RULE] 분기의 LINK만** <a>로 첨부(LINK는 선택 — 없으면 미첨부).
    JSON 파싱 실패 시(비-JSON 응답) 종전처럼 텍스트/HTML 그대로 사용(하위호환).
    """
    import html as _html

    def _esc(x):
        return _html.escape(str(x), quote=False)

    def _sent(t):
        """문장 끝 마침표 보정(이미 . ! ? 로 끝나면 그대로)."""
        t = str(t or '').strip()
        return t if (not t or t[-1] in '.!?…') else t + '.'

    data = _extract_json_obj(final_text)
    if not isinstance(data, dict):
        t = str(final_text or '').strip()
        return t if '<' in t else f'<div style="margin:3px 0;">{_esc(t)}</div>'

    # 불량 모드 검증 — [RULE] 모드명과 대조(공백 차이 허용, 부분 포함까지)
    mode_raw = data.get('defect_mode')
    mode_raw = str(mode_raw).strip() if isinstance(mode_raw, str) and str(mode_raw).strip().lower() not in ('null', 'none') else ''
    entry = None
    if mode_raw and modes:
        for _m in modes:
            _mm = _m.get('mode', '').strip()
            if _mm and (_mm == mode_raw or _mm in mode_raw or mode_raw in _mm):
                entry = _m
                break

    basis = data.get('basis_items') or []
    if isinstance(basis, str):
        basis = [basis]
    basis_txt = ', '.join(_esc(b) for b in basis if b)

    paras = []
    # ① 판정 문장 — 매칭된 [RULE] 모드만 표기(미매칭이면 모드명 미표기 = 수동 검토 안내)
    if entry:
        p = f'<b>{_esc(entry["mode"])}</b>(이)가 추정됩니다'
        p += f' (근거: <b>{basis_txt}</b>).' if basis_txt else '.'
        if entry.get('link'):
            p += f' <a href="{_html.escape(entry["link"], quote=True)}" target="_blank">관련 링크</a>'
    else:
        p = '지식 규칙(ANOMALY_KNOWLEDGE.md)에 매칭되는 불량 모드가 없어 <b>수동 검토가 필요</b>합니다'
        p += f' (이상 항목: <b>{basis_txt}</b>).' if basis_txt else '.'
    paras.append(p)

    # ② 현상/원인 서술 — 관찰 사실(phenomenon) + 종합 판단(summary)을 이어서 평문으로
    _body = ' '.join(_sent(_esc(data.get(k))) for k in ('phenomenon', 'summary') if data.get(k))
    if _body:
        paras.append(_body)

    # ③ 측정이상 추정(있을 때만) — 재측정 우선 안내
    _ms = data.get('meas_suspect')
    if isinstance(_ms, str) and _ms.strip() and _ms.strip().lower() not in ('null', 'none'):
        paras.append(f'<b>측정이상 가능성</b>: {_sent(_esc(_ms))} 불량 단정 전 <b>재측정으로 재현성 확인</b>을 우선하세요.')

    # ④ 확인/조치 — LLM actions + 매칭 [RULE]의 코멘트(comment, 있으면)
    _act = _esc(data.get('actions') or '').strip()
    if entry and entry.get('comment'):
        _cm = _esc(entry['comment'])
        if _cm not in _act:
            _act = (_act + ' ' if _act else '') + _sent(_cm)
    if _act:
        paras.append(f'<b>확인/조치</b>: {_sent(_act)}')

    return ''.join(f'<div style="margin:3px 0;">{p}</div>' for p in paras)


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


def _parse_chain_rules(text):
    """ANOMALY_RULES 마커 안의 '[RULE] 통합 체이닝 규칙' 파싱 — **판정 규칙의 단일 포맷**.

    측정순서 함수·CAT2 조건·다단계 분기(decision tree)·다중 note/link·산포 억제/비교까지
    '하나의 룰 포맷'으로 표현한다(별도 RULE:/MSEQ/DEFECT_TREE 섹션 없음 — 전부 통합).
    한 규칙은 `[RULE]` 헤더로 시작하고 아래 키(대소문자 무시)를 가진다:
      - `name:`    규칙 이름(선택 — 트레이스/finding 라벨용. 없으면 trigger로 표기)
      - `trigger:` 대상 항목(함수 spec_out/seq_*의 주체 & finding item)
      - `sev:`     critical|warning|이상|주의 (finding 심각도, 기본 critical)
      - `when:`    게이트 조건(참일 때만 규칙 활성. 비우면 항상 활성)
      - `when2:`, `when3:`, ... : 연쇄 분기 조건(when 만족 시 위에서부터 순차 평가)
      - `whenN_else:` : 가독성용 구분자(무시)
      - `note:`, `note2:`, ... / `link:`, `link2:`, ... : 분기별 불량 모드 문구/링크(여러 개 가능)
      - `suppress_disp: A,B,...` : 게이트 참일 때 나열 항목의 산포(주의) finding 억제(액션)
      - `compare_disp: A,B | C,D` : 게이트 참일 때 두 그룹 산포 비교 finding 생성(액션)
    분기 매핑: branch i(1-base) = 조건 `when{i+1}`(없으면 else) → `note{i}`/`link{i}`.
      즉 when2→note/link, when3→note2/link2, (else)→note3/link3. 조건식 함수는
      spec_out<op>n · seq_out(n) · seq_front_heavy · seq_mostly_dead(f) · all_sev(그룹...,level) ·
      sev(ITEM,level) · sev(ITEM)>=이상 · all_sev(...)>=이상 · disp_asc/desc(...) ·
      median_low/median_pctile(...) · *_cat2(...) — WHEN 원자 전부 when/whenN에서 공용.
    반환: [{'name','trigger','sev','when','branches':[{'cond'(None=else),'note','link'},...],
            'suppress_disp': [..]|None, 'compare_disp': ([..],[..])|None}, ...].
    """
    import re
    out = []
    if not text:
        return out
    _s = text.find('ANOMALY_RULES:start')
    _e = text.find('ANOMALY_RULES:end')
    if _s == -1 or _e == -1 or _e <= _s:
        return out
    _blocks = re.split(r'(?im)^\s*\[RULE\]\s*$', text[_s:_e])
    for _blk in _blocks[1:]:      # 첫 조각은 [RULE] 이전(무시)
        cur = {'name': '', 'trigger': '', 'sev': 'critical', 'when': '',
               'whens': {}, 'notes': {}, 'links': {},
               'suppress_disp': None, 'compare_disp': None}
        for line in _blk.splitlines():
            if 'ANOMALY_RULES' in line:
                continue
            m = re.match(r'\s*([A-Za-z_]+[0-9]*)\s*[:：]\s*(.*)$', line)
            if not m:
                continue
            key = m.group(1).lower()
            val = m.group(2).strip().strip('"').strip("'").strip()
            if key == 'name':
                cur['name'] = val
            elif key == 'trigger':
                cur['trigger'] = val
            elif key == 'sev':
                cur['sev'] = val.lower()
            elif key == 'when':
                cur['when'] = val
            elif re.match(r'when\d+_else$', key):
                continue
            elif re.match(r'when\d+$', key):
                cur['whens'][int(key[4:])] = val
            elif key == 'note':
                cur['notes'][1] = val
            elif re.match(r'note\d+$', key):
                cur['notes'][int(key[4:])] = val
            elif key == 'link':
                cur['links'][1] = val
            elif re.match(r'link\d+$', key):
                cur['links'][int(key[4:])] = val
            elif key == 'suppress_disp':
                cur['suppress_disp'] = [t.strip() for t in val.split(',') if t.strip()]
            elif key == 'compare_disp':
                _parts = val.split('|')
                if len(_parts) == 2:
                    cur['compare_disp'] = (
                        [t.strip() for t in _parts[0].split(',') if t.strip()],
                        [t.strip() for t in _parts[1].split(',') if t.strip()])
        if not (cur['trigger'] or cur['when'] or cur['notes']
                or cur['suppress_disp'] or cur['compare_disp']):
            continue
        _max = max(list(cur['notes'].keys()) + [0])
        branches = []
        for i in range(1, _max + 1):
            branches.append({'cond': cur['whens'].get(i + 1),
                             'note': cur['notes'].get(i, ''),
                             'link': cur['links'].get(i, '')})
        if not branches and cur['when'] and not (cur['suppress_disp'] or cur['compare_disp']):
            branches = [{'cond': None, 'note': '', 'link': ''}]
        out.append({'name': cur['name'], 'trigger': cur['trigger'], 'sev': cur['sev'],
                    'when': cur['when'], 'branches': branches,
                    'suppress_disp': cur['suppress_disp'], 'compare_disp': cur['compare_disp']})
    return out


# ──────────────────────────────────────────────────────────────────────
# 자연어 규칙(NL_RULES) → [RULE] 자동 컴파일
#   엔지니어는 ANOMALY_KNOWLEDGE.md의 NL_RULES 마커 사이에 '자연어'로 규칙을 적고,
#   코드가 LLM 1회 호출로 [RULE] 체이닝 포맷으로 변환(컴파일)해 ANOMALY_RULES에 주입한다.
#   - 변환 결과는 코드가 정적 검증(파싱 + 조건 원자 문법 확인) → 실패 시 오류 피드백으로 1회 재시도
#     → 그래도 실패한 블록은 제외(유효 블록만 적용).
#   - 캐시: RUN/AI/nl_rules_compiled.json (자연어 원문 sha256 일치 시 LLM 재호출 없음 →
#     결정론적/감사 가능. 엔지니어는 이 파일에서 '무엇으로 컴파일됐는지' 확인).
#   - LLM 미연결 + 캐시 없음 → 미적용(수기 [RULE]만 동작 — AI-optional 원칙 유지).
# ──────────────────────────────────────────────────────────────────────

# [RULE]에서 사용 가능한 키/조건 함수 전체 카탈로그 — NL 컴파일 프롬프트와 문서에 공용.
RULE_FUNCTION_SPEC = """\
[RULE] 블록 키(대소문자 무시):
  name: 규칙 이름(선택 — 트레이스/finding 라벨. 없으면 trigger로 표기)
  trigger: 대상 항목(spec_out/seq_* 함수의 주체 & finding item)
  sev: critical|warning (finding 심각도, 기본 critical)
  when: 게이트 조건(참일 때만 규칙 활성. 비우면 항상 활성)
  when2:, when3:, ... : 연쇄 분기 조건(when 통과 후 위에서부터 순차 평가, 먼저 만족한 분기 1개만)
  whenN_else: : 가독성용 구분자(무시됨)
  note:, note2:, ... : 분기별 불량 모드 문구(매핑: when2→note, when3→note2, else→마지막 noteN)
  link:, link2:, ... : 분기별 대시보드 링크(선택)
  suppress_disp: A,B,... : 게이트 참일 때 나열 항목의 산포(주의) finding 억제(액션)
  compare_disp: A,B | C,D : 게이트 참일 때 두 그룹 산포 비교 finding 생성(액션)

조건식(when/whenN 공용): 원자를 ' AND ' / ' OR '로 연결(OR로 묶인 AND 그룹).
사용 가능한 조건 원자/함수(정확히 이 문법만 — 임의 함수 생성 금지):
  spec_out >= n            : trigger 항목의 spec-out pt 수 비교(연산자 >= <= == < >)
  seq_out(n)               : 측정순서(chip_x 먼저 증가→chip_y 증가)상 연속 spec-out ≥ n
  seq_mostly_dead(f)       : 측정순서 시퀀스의 spec-out 비율 ≥ f (0~1)
  seq_front_heavy          : 앞 절반 이탈 많음(≥0.6) + 뒤 절반 양호(≤0.2)
  sev(ITEM, critical)      : 항목 등급 ≥ 지정 등급 (critical|warning|이상|주의|참고|info)
  sev(ITEM) >= 이상        : 항목 등급 비교(연산자 >= <= == < >, 등급 이상|주의|참고. 미측정=참고)
  all_sev(A, B, critical)  : 나열 그룹(CAT2 또는 항목) '모두' ≥ 지정 등급
  all_sev(A, B) >= 이상    : 위와 동일(연산자 표기, 등급 이상|주의)
  sev_cat2(CAT2) >= 이상   : CAT2 그룹의 최대 항목 등급 비교
  all_sev_cat2(A, B) >= 이상 : 나열 CAT2 '모두'에서 해당 등급 이상
  disp_desc(A, B, C)       : 산포배수가 나열 순서대로 감소(A가 최대) / disp_asc(...)는 증가
  disp_desc_cat2(A, B)     : CAT2별 최대 산포배수가 순서대로 감소 / disp_asc_cat2(...)는 증가
  median_low(ITEM)         : target median이 제품 대비 매우 낮음(임계 σ는 설정값)
  median_high(ITEM)        : target median이 제품 대비 매우 높음(median_low의 대칭)
  median_pctile(ITEM) <= 15 : target median의 모집단 내 백분위(%) 비교(연산자 >= <= < >).
                              하위 N% = <=N, 상위 N% = >=100-N
  spec_out_pt(ITEM) >= n   : 지정 항목의 spec-out pt 수 비교(trigger 없이 임의 항목에 사용 가능)
  spec_out_wafers(ITEM) >= n : spec-out이 발생한 wafer 수 비교
  spec_out_ratio(ITEM) >= f  : wafer 최고 이탈 비율(그 wafer의 out pt/측정 pt, 0~1) 비교
  disp(ITEM) >= x          : 항목의 worst wafer 산포배수를 숫자로 직접 비교
  disp_cat2(CAT2) >= x     : CAT2 그룹 최대 산포배수를 숫자로 직접 비교
  median_dev_sigma(ITEM) >= x : worst wafer median 이탈 σ(제품 wafer 기준) 비교
  pattern(ITEM, Edge ring) : 특이맵 라벨 부분일치(예: Edge ring/줄성/Center 집중 —
                             특이맵 판정(anomaly_pattern_rules) 활성 시에만 라벨이 생성됨)
  zone_share(ITEM, Edge) >= f : spec-out 좌표 중 해당 zone(Edge|Middle|Center) 비율(0~1)
  repeat_shot(ITEM)        : 여러 wafer에서 '동일 shot 반복' 코멘트 존재(특이맵 판정 활성 시)
  repeat_similar(ITEM)     : 여러 wafer에서 '유사 위치 반복' 코멘트 존재(특이맵 판정 활성 시)
  meas_overlap(PCHK명) >= n : 그 PCHK와 동일 shot에서 다른 항목이 함께 spec-out인 겹침 수 비교
  measured(ITEM)           : 항목이 target lot에서 측정됨(미측정 항목 가드용)
  count_sev(critical) >= n : 해당 등급 이상(critical|warning)인 항목의 '개수' 비교(전 항목 대상)
등급 의미: 이상(critical)=spec 이탈 pt 존재 / 주의(warning)=wafer 산포가 보통 wafer 대비 임계배수 초과.
항목명/CAT2명은 원 이름(ALIAS)·표시명 둘 다 인식된다(자연어에 적힌 표기 그대로 사용)."""

# 자연어 표현 → 조건 원자 변환 지침 — ANOMALY_KNOWLEDGE.md의 '쓸 수 있는 조건 표현' 표와 1:1.
#   (md는 사람용 가이드, 이 상수는 같은 매핑을 LLM 컴파일 프롬프트에 주입해 변환을 결정적으로 만든다.)
NL_PATTERN_HINTS = """\
자연어 표현 → 변환 지침(md 작성 가이드와 동일한 매핑):
  "A와 B가 둘 다 spec 이탈이면"        → when: all_sev(A, B, critical)
  "X 카테고리 전체가 이상이면"         → when: sev_cat2(X)>=이상
  "A가 spec 이탈이면"                  → when: sev(A, critical)
  "A의 spec 이탈이 n개 이상이면"       → trigger: A + when: spec_out >= n
  "median이 모집단 하위 N% 이내면"     → when: median_pctile(ITEM)<=N   (상위 N%는 >=100-N)
  "A가 주의(산포 확대)면"              → when: sev(A, warning)
  "산포가 A > B > C 순으로 크면"       → when: disp_desc(A, B, C)
  "측정 순서상 연속 n개 이상 이탈하면" → trigger: 항목 + when(또는 분기 조건): seq_out(n)
  "측정점의 N% 이상이 이탈하면"        → seq_mostly_dead(N/100)
  "측정 앞부분에 이탈이 몰려 있으면"   → seq_front_heavy
  "~이고/그리고 ~이면"                 → 조건을 ' AND '로 결합 / "~이거나/또는" → ' OR '
  "…일 때, ~이면 \"X\", 아니면 \"Y\""  → when(게이트) + when2/note("X") + else 마지막 note("Y") (분기)
  "…의 산포 주의 언급은 하지 마"       → suppress_disp: 항목 (조건은 when에)
  "…그룹과 …그룹의 산포를 비교"        → compare_disp: A,B | C,D
  "A의 spec 이탈이 n개 이상"(임의 항목) → spec_out_pt(A) >= n
  "A의 이탈 wafer가 n매 이상"          → spec_out_wafers(A) >= n
  "어느 wafer의 이탈 비율이 N% 이상"   → spec_out_ratio(A) >= N/100
  "A의 산포가 x배 이상"                → disp(A) >= x
  "X 카테고리 산포가 x배 이상"         → disp_cat2(X) >= x
  "median이 매우 높으면/낮으면"        → median_high(A) / median_low(A)
  "wafer median이 xσ 이상 이탈"        → median_dev_sigma(A) >= x
  "특이맵이 Edge ring(라벨)이면"       → pattern(A, Edge ring)
  "이탈의 N% 이상이 Edge(zone)면"      → zone_share(A, Edge) >= N/100
  "여러 wafer에서 같은 자리 반복이면"  → repeat_shot(A) (비슷한 자리면 repeat_similar)
  "PCHK와 동일 shot 겹침이 n개 이상"   → meas_overlap(PCHK명) >= n
  "A가 측정된 경우에만"                → measured(A) AND ...
  "이상 항목이 n개 이상이면"           → count_sev(critical) >= n
  "(주의)" 표기가 있으면               → sev: warning (없으면 critical)
  "링크: URL"                          → link: "URL" (해당 분기의 link)
판정명("큰따옴표 문구")은 note:에 그대로 넣는다. 따옴표가 없으면 문맥에서 짧은 판정 문구를 만들어 note에 넣는다."""

# 조건 원자 정적 검증 패턴 — _eval_chain_atom/_eval_atom의 인식 문법과 1:1 미러.
_ATOM_VALID_PATTERNS = [
    r'spec_out\s*(>=|<=|==|<|>)\s*[\d.]+$',
    r'seq_out\(\s*[\d.]+\s*\)$',
    r'seq_mostly_dead\(\s*[\d.]+\s*\)$',
    r'seq_front_heavy$',
    r'sev\([^,()]+,\s*(critical|warning|이상|주의|참고|info)\s*\)$',
    r'sev\([^()]+\)\s*(>=|<=|==|<|>)\s*(이상|주의|참고)$',
    r'sev_cat2\([^()]+\)\s*(>=|<=|==|<|>)\s*(이상|주의|참고)$',
    r'all_sev_cat2\([^()]+\)\s*>=\s*(이상|주의)$',
    r'all_sev\([^()]+,\s*(critical|warning|이상|주의|참고|info)\s*\)$',
    r'all_sev\([^()]+\)\s*>=\s*(이상|주의)$',
    r'disp_(desc|asc)(_cat2)?\([^()]+\)$',
    r'median_low\([^()]+\)$',
    r'median_pctile\([^()]+\)\s*(>=|<=|<|>)\s*[\d.]+$',
    # ── 확장 원자 (평가기 _eval_atom 확장분과 1:1) ──
    r'(spec_out_pt|spec_out_wafers|spec_out_ratio|disp|median_dev_sigma|meas_overlap)\([^()]+\)\s*(>=|<=|==|<|>)\s*[\d.]+$',
    r'disp_cat2\([^()]+\)\s*(>=|<=|==|<|>)\s*[\d.]+$',
    r'median_high\([^()]+\)$',
    r'pattern\([^,()]+,\s*[^()]+\)$',
    r'zone_share\([^,()]+,\s*(?i:edge|middle|center)\s*\)\s*(>=|<=|<|>)\s*[\d.]+$',
    r'repeat_(shot|similar)\([^()]+\)$',
    r'measured\([^()]+\)$',
    r'count_sev\((critical|warning|이상|주의)\)\s*(>=|<=|==|<|>)\s*[\d.]+$',
    # ── trigger 기준 median/stddev 원자 (우변: 숫자 또는 spec_high/spec_low[*계수]) ──
    r'sev\s*(>=|<=|==|<|>)\s*[\d.]+$',
    r'(stddev|std)\s*(>=|<=|==|<|>)\s*([\d.]+|spec_(high|low)(\s*\*\s*[\d.]+)?)$',
    r'median\s*(>=|<=|==|<|>)\s*([\d.]+|spec_(high|low)(\s*\*\s*[\d.]+)?)$',
]


def _atom_valid(atom):
    """조건 원자 하나가 [RULE] 평가기가 인식하는 문법인지 정적 확인."""
    import re
    a = atom.strip()
    if not a:
        return False
    return any(re.match(p, a) for p in _ATOM_VALID_PATTERNS)


def _cond_valid(expr):
    """조건식(AND/OR 결합) 전체의 원자 문법 확인. (빈 식은 '항상 참'으로 유효)"""
    import re
    if not (expr or '').strip():
        return True
    for _grp in re.split(r'\s+OR\s+', expr):
        for _a in re.split(r'\s+AND\s+', _grp):
            if _a.strip() and not _atom_valid(_a):
                return False
    return True


def _validate_rules_text(rules_text):
    """[RULE] 블록 텍스트를 파싱+정적 검증. 반환 (규칙 수, 오류 목록)."""
    wrapped = f"ANOMALY_RULES:start\n{rules_text}\nANOMALY_RULES:end"
    rules = _parse_chain_rules(wrapped)
    errors = []
    if not rules:
        errors.append("[RULE] 블록이 하나도 파싱되지 않음")
        return 0, errors
    for _i, _r in enumerate(rules, start=1):
        _nm = _r.get('name') or _r.get('trigger') or f"#{_i}"
        if not (_r.get('branches') or _r.get('suppress_disp') or _r.get('compare_disp')):
            errors.append(f"규칙 {_nm}: 분기(note)도 액션(suppress/compare_disp)도 없음")
        if _r.get('branches') and not any((_b.get('note') or '').strip() for _b in _r['branches']):
            errors.append(f"규칙 {_nm}: note(불량 모드 문구)가 비어 있음")
        if not _cond_valid(_r.get('when', '')):
            errors.append(f"규칙 {_nm}: when 조건 '{_r.get('when','')}' 인식 불가(지원 원자 아님)")
        for _b in _r.get('branches', []):
            if _b.get('cond') is not None and not _cond_valid(_b['cond']):
                errors.append(f"규칙 {_nm}: 분기 조건 '{_b['cond']}' 인식 불가(지원 원자 아님)")
    return len(rules), errors


def _keep_valid_rule_blocks(rules_text):
    """블록 단위로 재검증해 유효한 [RULE] 블록만 남긴다. 반환 (텍스트, 유효 수, 제외 수)."""
    import re
    _blocks = re.split(r'(?im)^\s*\[RULE\]\s*$', rules_text)
    _ok = []
    _dropped = 0
    for _blk in _blocks[1:]:
        _n, _errs = _validate_rules_text('[RULE]\n' + _blk)
        if _n == 1 and not _errs:
            _ok.append('[RULE]\n' + _blk.strip())
        else:
            _dropped += 1
    return '\n\n'.join(_ok), len(_ok), _dropped


def _extract_nl_rules(text):
    """ANOMALY_KNOWLEDGE.md의 NL_RULES 마커 사이 자연어 규칙 원문을 추출(마커 라인 제외)."""
    if not text:
        return ''
    _s = text.find('NL_RULES:start')
    _e = text.find('NL_RULES:end')
    if _s == -1 or _e == -1 or _e <= _s:
        return ''
    lines = [l for l in text[_s:_e].splitlines()
             if 'NL_RULES' not in l and l.strip() not in ('', '<!--', '-->')]
    return '\n'.join(lines).strip()


def _strip_code_fences(text):
    """LLM 응답의 ``` 코드펜스 제거(있으면 첫 펜스 안쪽만, 없으면 원문)."""
    import re
    t = str(text or '').strip()
    m = re.search(r'```[a-zA-Z]*\n(.*?)```', t, flags=re.S)
    return m.group(1).strip() if m else t


def compile_nl_rules(knowledge_text, llm_fn, cache_dir='RUN/AI'):
    """NL_RULES(자연어 규칙) → [RULE] 텍스트 컴파일. 컴파일 결과 텍스트 반환(없으면 '').

    구조 = 'LLM 1회 생성 → 코드(결정론) 검증 → 실패 시 오류 피드백 재시도 1회 → 유효 블록만 적용'.
    다단계 AI(의도 분해/항목 매핑/조합을 별도 호출)로 나누지 않는 이유: 변환 대상 DSL이 작고
    검증기가 코드로 존재하므로, 생성-검증 루프가 다단계 분해보다 단순하고 오류 전파가 없다
    (검증 실패 시 정확한 오류 문구가 재시도 프롬프트로 들어가 자가 수정됨).
    캐시(RUN/AI/nl_rules_compiled.json)로 같은 자연어 원문엔 LLM을 다시 호출하지 않는다.
    """
    import hashlib
    import json as _json
    import os
    from datetime import datetime as _dt

    nl = _extract_nl_rules(knowledge_text)
    if not nl:
        return ''
    src_sha = hashlib.sha256(nl.encode('utf-8')).hexdigest()
    cache_path = os.path.join(cache_dir, 'nl_rules_compiled.json')

    # ── 캐시 히트: 자연어 원문이 그대로면 LLM 호출 없이 이전 컴파일 결과 재사용 ──
    try:
        with open(cache_path, encoding='utf-8') as _cf:
            _c = _json.load(_cf)
        if _c.get('source_sha') == src_sha and _c.get('compiled'):
            print(f"[NL RULES] 캐시 사용({_c.get('n_rules', '?')}개 규칙) — 자연어 규칙 변경 없음")
            return _c['compiled']
    except Exception:
        pass

    if llm_fn is None:
        print("[WARN] NL RULES: 자연어 규칙이 있으나 LLM 미연결·캐시 없음 → 이번 실행 미적용(수기 [RULE]만 동작)")
        return ''

    _system = (
        "당신은 규칙 DSL 컴파일러입니다. 아래 '자연어 규칙' 각각을 [RULE] 블록 하나로 변환하세요.\n"
        "출력은 [RULE] 블록들만(설명문/코드펜스/번호 금지). 자연어 규칙의 순서를 유지하세요.\n"
        "- name: 에는 자연어 규칙을 요약한 짧은 이름을, note: 에는 판정 시 표기할 불량 모드 문구를 적으세요\n"
        "  (자연어에 판정명/코멘트가 있으면 그대로 사용).\n"
        "- 항목명/CAT2명은 자연어에 적힌 표기 그대로 사용하세요(임의 변경 금지).\n"
        "- 아래 카탈로그에 있는 키·조건 원자만 사용하세요. 카탈로그로 표현할 수 없는 규칙은\n"
        "  블록을 만들지 말고 '# 변환불가: <이유>' 주석 한 줄만 남기세요.\n\n"
        + RULE_FUNCTION_SPEC + "\n\n" + NL_PATTERN_HINTS)
    _user = f"[자연어 규칙]\n{nl}"

    compiled = _strip_code_fences(llm_fn(_system, _user))
    n, errs = _validate_rules_text(compiled)
    if errs:
        # 검증 오류를 그대로 피드백해 1회 재시도(자가 수정)
        _retry_sys = (_system + "\n\n직전 변환에 아래 오류가 있었습니다. 오류를 수정해 "
                      "전체 [RULE] 블록을 처음부터 다시 출력하세요:\n- " + "\n- ".join(errs))
        compiled = _strip_code_fences(llm_fn(_retry_sys, _user))
        n, errs = _validate_rules_text(compiled)
    if errs:
        compiled, n, _dropped = _keep_valid_rule_blocks(compiled)
        print(f"[WARN] NL RULES: 일부 규칙 변환 실패 → 유효 {n}개만 적용, {_dropped}개 제외 ({'; '.join(errs[:3])})")
    if n == 0 or not compiled.strip():
        print("[WARN] NL RULES: 유효한 [RULE] 변환 결과 없음 → 미적용")
        return ''

    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as _cf:
            _json.dump({'source_sha': src_sha, 'source_nl': nl, 'compiled': compiled,
                        'n_rules': n, 'errors': errs,
                        'generated': _dt.now().strftime('%Y-%m-%d %H:%M:%S')},
                       _cf, ensure_ascii=False, indent=2)
    except Exception as _we:
        print(f"[WARN] NL RULES 캐시 저장 실패: {_we}")
    print(f"[NL RULES] 자연어 규칙 → [RULE] {n}개 컴파일 완료 (검증 통과, 캐시: {cache_path})")
    return compiled


def inject_compiled_rules(knowledge_text, compiled_text):
    """컴파일된 [RULE] 텍스트를 ANOMALY_RULES 마커 '안'(end 직전)에 주입한 지식 텍스트 반환.

    수기 [RULE]들 뒤에 붙으므로 우선순위는 수기 규칙이 위(먼저 매칭 우선)다.
    마커가 없으면 새 ANOMALY_RULES 섹션을 문서 끝에 추가한다.
    """
    if not (compiled_text or '').strip():
        return knowledge_text
    _hdr = "\n# ── 자연어 규칙(NL_RULES) 자동 컴파일 결과 — 원문/검증은 RUN/AI/nl_rules_compiled.json ──\n"
    _mk = 'ANOMALY_RULES:end'
    i = (knowledge_text or '').find(_mk)
    if i == -1:
        return ((knowledge_text or '') + "\n<!-- ANOMALY_RULES:start -->" + _hdr
                + compiled_text.strip() + "\n<!-- ANOMALY_RULES:end -->\n")
    j = knowledge_text.rfind('<!--', 0, i)
    ins = j if j != -1 else i
    return knowledge_text[:ins] + _hdr + compiled_text.strip() + "\n\n" + knowledge_text[ins:]


def analyze_commonality(merged_df, target_lot_id, metrics_dict, spec_data,
                        main_vehicle=None, config=None, reformatter=None,
                        knowledge_text="", item_stats_out=None, rule_trace_out=None):
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
    item_stats_out : dict|None  전달 시 항목별 통계 요약({item: {...}})을 채워 반환
                                (AI 해석의 [항목 통계] 입력 — interpret_with_ai로 전달)
    rule_trace_out : list|None  전달 시 '전체 anomaly rule 체크 결과' 추적 리스트를 채워 반환.
                                각 원소 {kind, name, cond, matched(bool), result, note} —
                                모든 규칙(지식/불량모드/[RULE] 체이닝)을 순회한 매칭/해당없음 기록.

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
    _rule_trace = []   # 전체 anomaly rule 체크 추적(매칭/해당없음) — rule_trace_out으로 반환
    if merged_df is None or len(merged_df) == 0 or not metrics_dict:
        if isinstance(rule_trace_out, list):
            rule_trace_out.extend(_rule_trace)
        return findings

    col_lot = _pick_col(merged_df, 'FAB_LOT_ID', 'fab_lot_id')
    col_waf = _pick_col(merged_df, 'WAFER_ID', 'wafer_id')
    col_time = _pick_col(merged_df, 'TKOUT_TIME', 'tkout_time')
    col_mask = _pick_col(merged_df, 'MASK', 'mask')
    col_x = _pick_col(merged_df, 'CHIP_X_ADJ', 'CHIP_X_POS', 'chip_x_pos')
    col_y = _pick_col(merged_df, 'CHIP_Y_ADJ', 'CHIP_Y_POS', 'chip_y_pos')
    col_pgm = _pick_col(merged_df, 'PGM(pt)')
    col_rad = _pick_col(merged_df, 'Chip_Radius', 'chip_radius')   # Data Extractor radius(mm)

    items = [it for it in metrics_dict.keys() if it in merged_df.columns]

    # 모집단: main_vehicle만 (lot간 비교용)
    pop = merged_df
    if col_mask and main_vehicle is not None:
        _m = merged_df[merged_df[col_mask] == main_vehicle]
        if len(_m) > 0:
            pop = _m
    tgt = pop[pop[col_lot] == target_lot_id] if col_lot else pop

    # ── Data Extractor radius 매핑: MASK==main_vehicle 행의 (좌표)->Chip_Radius(mm) ──
    #   spec-out chip의 zone(Center/Middle/Edge)은 이 실제 radius로 판정한다
    #   (sqrt(x^2+y^2) 좌표거리 대신 Data Extractor의 Chip_Radius 사용).
    _coord_radius = {}
    if col_rad and col_x and col_y and col_rad in merged_df.columns:
        try:
            _rref = merged_df
            if col_mask and main_vehicle is not None:
                _mv = merged_df[merged_df[col_mask] == main_vehicle]
                if len(_mv) > 0:
                    _rref = _mv
            _rref = _rref[[col_x, col_y, col_rad]].dropna()
            if len(_rref) > 0:
                _g = _rref.groupby([col_x, col_y])[col_rad].mean()
                _coord_radius = {(float(_k[0]), float(_k[1])): float(_v)
                                 for _k, _v in _g.items()}
        except Exception as _re:
            print(f"[anomaly] Chip_Radius 좌표 매핑 실패: {_re}")
            _coord_radius = {}

    # ── 특이맵(공간 패턴) 판정용 제품 전체 좌표 집합 ──
    #   설정파일 기반 Chip_Radius 매핑(_coord_radius)이 있으면 그 좌표를,
    #   없으면 모집단 측정 좌표(unique)를 사용 — 제품별 좌표계가 달라도 정규화로 동작.
    _pat_all_xy = list(_coord_radius.keys())
    if not _pat_all_xy and col_x and col_y:
        try:
            _pc = pop[[col_x, col_y]].dropna().drop_duplicates()
            _pat_all_xy = [(float(a), float(b)) for a, b in zip(_pc[col_x], _pc[col_y])]
        except Exception:
            _pat_all_xy = []
    _pat_rules = cfg('anomaly_pattern_rules', None) or None   # None/빈 → 특이맵 판정 안 함(하드코딩 기본 없음)
    _pat_opt = cfg('anomaly_pattern_thresholds', {}) or {}    # 전역 옵션(min_pts, y_positive_up)
    # NOTE: 측정 순서 기반 패턴은 별도 MSEQ 설정/섹션 없이 [RULE]의 seq_* 조건 함수로만 판정한다.

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

    def _specout_extra(it, lo, hi, max_positions=20):
        """spec-out chip의 PGM(pt) 목록·radius zone 분포·위치 예시·공간 패턴을 반환.

        반환 (pgms, zones, positions, pattern, pattern_stats, commonality):
        - pgms      : spec-out chip의 PGM(pt) 목록(중복 제거)
        - zones     : {Center/Middle/Edge: 개수}
        - positions : [{'wafer','x','y','pgm'}, ...] 이상 pt의 실제 위치(최대 max_positions개).
                      AI가 PCHK와 '동일 wafer·좌표·PGM(pt)' 겹침(측정이상 추정)을 대조하는 입력.
        - pattern   : 특이맵 라벨(classify_specout_pattern — Edge ring/줄성/k시 방향 등).
                      **wafer 게이트**: 이상 wafer가 1~2개면 spec-out 총 gate_few_wafer_min_pts(4)pt
                      이상일 때만 판정(미만이면 '' — 소수 pt 노이즈로 인한 오분류 방지).
        - pattern_stats : 패턴 판정에 쓴 수치/게이트 사유(basis 기록용)
        - commonality   : 이상 wafer가 repeat_min_wafers(3)개 이상일 때 wafer간 반복 코멘트 —
                      '동일 shot 반복'(같은 좌표가 3개 wafer 이상 spec-out) 또는
                      'wafer간 유사 위치 반복'(out 좌표의 절반 이상이 2개 wafer 이상 겹침). 없으면 ''.
        """
        cols = [c for c in [col_waf, col_x, col_y, col_pgm, it] if c and c in tgt.columns]
        if it not in tgt.columns or not cols:
            return [], {}, [], '', {}, '', ''
        _sub = tgt[cols].dropna(subset=[it])
        if len(_sub) == 0:
            return [], {}, [], '', {}, '', ''
        _v = pd.to_numeric(_sub[it], errors='coerce')
        _om = pd.Series(False, index=_sub.index)
        if lo is not None: _om = _om | (_v < lo)
        if hi is not None: _om = _om | (_v > hi)
        _so = _sub[_om.values]
        if len(_so) == 0:
            return [], {}, [], '', {}, '', ''
        # PGM(pt) 뒤 Duplicate_Count 기본값('_1.0'/'_1') 접미사는 불필요 → 제거(중복>1은 유지)
        def _pgm_clean(p):
            return re.sub(r'_1(?:\.0+)?$', '', str(p))
        pgms = ([_pgm_clean(p) for p in _so[col_pgm].dropna().unique()]
                if col_pgm and col_pgm in _so.columns else [])
        # zone: Data Extractor Chip_Radius(mm)를 좌표로 조회해 Center/Middle/Edge 판정
        zones = {}
        if _coord_radius and col_x and col_y and col_x in _so.columns and col_y in _so.columns:
            for _xx, _yy in zip(_so[col_x], _so[col_y]):
                try:
                    _rr = _coord_radius.get((float(_xx), float(_yy)))
                except (ValueError, TypeError):
                    _rr = None
                if _rr is not None:
                    _z = _zone_of(_rr)
                    zones[_z] = zones.get(_z, 0) + 1
        # 이상 pt 위치 목록 (wafer·좌표·PGM(pt)) — 프롬프트 크기 제한 위해 상한 적용
        positions = []
        _n_more = 0
        if col_waf in _so.columns and col_x and col_y \
                and col_x in _so.columns and col_y in _so.columns:
            for _ridx, _row in _so.iterrows():
                if len(positions) >= max_positions:
                    _n_more += 1
                    continue
                _w = _waf_int(_row[col_waf])
                try:
                    _xx, _yy = int(_row[col_x]), int(_row[col_y])
                except Exception:
                    _xx, _yy = _row[col_x], _row[col_y]
                positions.append({
                    'wafer': _w if _w is not None else _row[col_waf],
                    'x': _xx, 'y': _yy,
                    'pgm': _pgm_clean(_row[col_pgm])
                           if col_pgm and col_pgm in _so.columns and pd.notna(_row[col_pgm]) else ''})
        if _n_more:
            positions.append({'note': f'외 {_n_more}pt 생략(전체는 anomaly_basis 참조)'})
        # 특이맵(공간 패턴) 분류 — wafer간 중복 좌표는 제거하고 lot 전체 관점으로 판정
        pattern, pattern_stats, commonality = '', {}, ''
        # _pat_rules(My_config.anomaly_pattern_rules)가 설정된 경우에만 특이맵(공간 패턴) 판정
        if _pat_rules and col_x and col_y and col_x in _so.columns and col_y in _so.columns and _pat_all_xy:
            try:
                _oxy = [(a, b) for a, b in zip(_so[col_x], _so[col_y])
                        if pd.notna(a) and pd.notna(b)]
                _n_wf_out = int(_so[col_waf].nunique()) if col_waf in _so.columns else 1
                _total_pts = len(_oxy)
                # wafer 게이트: 이상 wafer 1~2개면 spec-out 4pt 이상일 때만 특이맵 판정
                _gate_wf = int(_pat_opt.get('gate_few_wafer_max', 2))
                _gate_pts = int(_pat_opt.get('gate_few_wafer_min_pts', 4))
                if _n_wf_out <= _gate_wf and _total_pts < _gate_pts:
                    pattern_stats = {'gated': f'이상 wafer {_n_wf_out}개·{_total_pts}pt'
                                              f'(<{_gate_pts}pt) → 특이맵 판정 보류',
                                     'n_out_wafers': _n_wf_out}
                else:
                    pattern, pattern_stats = classify_specout_pattern(
                        _oxy, _pat_all_xy, _coord_radius,
                        rules=_pat_rules, options=_pat_opt)
                    pattern_stats['n_out_wafers'] = _n_wf_out
                # 이상 wafer가 3개 이상이면 (pt 수가 적어도) wafer간 반복성 코멘트.
                #   단 채택 패턴이 '전면성(global)'이면 생략 — 전 좌표가 out이라 반복이 자명(노이즈).
                _adopted_type = next((r.get('type') for r in (pattern_stats.get('rules') or [])
                                      if r.get('passed')), '')
                _rep_min = int(_pat_opt.get('repeat_min_wafers', 3))
                if _n_wf_out >= _rep_min and _adopted_type != 'global' and col_waf in _so.columns:
                    _by_coord = {}
                    for _w, _a, _b in zip(_so[col_waf], _so[col_x], _so[col_y]):
                        if pd.notna(_a) and pd.notna(_b):
                            _by_coord.setdefault((float(_a), float(_b)), set()).add(_w)
                    _rep = sorted(((k, len(v)) for k, v in _by_coord.items()
                                   if len(v) >= _rep_min), key=lambda z: -z[1])
                    if _rep:
                        _top = ', '.join(f"({x:g},{y:g})×{c}wf" for (x, y), c in _rep[:3])
                        commonality = (f"동일 shot 반복: {len(_rep)}개 좌표가 "
                                       f"{_rep_min}개 wafer 이상에서 spec-out — {_top}"
                                       + (' 외' if len(_rep) > 3 else ''))
                    elif _by_coord:
                        _n_multi = sum(1 for v in _by_coord.values() if len(v) >= 2)
                        _frac = _n_multi / len(_by_coord)
                        if _frac >= float(_pat_opt.get('similar_overlap_frac', 0.5)):
                            commonality = (f"wafer간 유사 위치 반복: out 좌표의 {_frac:.0%}가 "
                                           f"2개 wafer 이상에서 겹침({_n_wf_out}개 wafer 발생)")
                    if commonality:
                        pattern_stats['commonality'] = commonality
            except Exception as _pe:
                print(f"[anomaly] 특이맵 분류 실패({it}): {_pe}")

        # NOTE: 측정 순서 기반 판정은 [RULE]의 seq_out/seq_mostly_dead/seq_front_heavy 조건 함수가
        #       담당한다(_seq_metrics 지표 사용). 별도 측정순서 라벨은 생성하지 않는다.
        return pgms, zones, positions, pattern, pattern_stats, commonality

    # PCHK 계열도 '동일한 index 항목'으로 같은 루프에서 함께 분석하고, 판정도 동일하게 적용한다.
    #   - 비차트(REPORT ORDER 없음)라 metrics_dict엔 없지만 merged_df엔 컬럼으로 존재 → items에 합류.
    #   - spec-out이면 다른 Index와 똑같이 '이상(CRITICAL)'으로 본다(별도 MEAS_SUSPECT 없음).
    #   - 단, '동일 shot 다른 항목 동시 spec-out' 겹침 신호는 basis에만 기록해 AI 측정이상 추정에 넘긴다.
    pchk_aliases = []
    cat2_map = {}   # ALIAS → CAT2 (AI Triage의 '같은 CAT2끼리 그룹핑' 입력용)
    try:
        if reformatter is not None and 'ALIAS' in reformatter.columns:
            for _, r in reformatter.iterrows():
                a = r.get('ALIAS')
                if pd.isna(a):
                    continue
                _c2 = r.get('CAT2')
                if pd.notna(_c2) and str(_c2).strip():
                    cat2_map[a] = str(_c2).strip()
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
    _meas_only = set()   # 제외 키워드에 걸린 PCHK — 이상/주의 '판정'에선 제외하되,
    #                      spec-out·동일 shot 겹침은 계산해 '측정이상 추정(NOTICE)' 신호로만 산출.
    #                      (PCHK를 통째로 빼면 AI가 측정이상 추정을 할 수 없게 되므로 신호는 유지)
    if _excl:
        _n0 = len(items)
        _keep = []
        for it in items:
            if item_excluded(it, _excl):
                if it in pchk_set:
                    _meas_only.add(it)
                    _keep.append(it)      # 루프에 남겨 겹침 신호만 계산
            else:
                _keep.append(it)
        items = _keep
        pchk_set = {a for a in pchk_set if a in items}
        if len(items) < _n0:
            print(f"[anomaly] anomaly_exclude_items로 {_n0 - len(items)}개 항목 통계분석 제외")
        if _meas_only:
            print(f"[anomaly] 제외 키워드 PCHK {len(_meas_only)}개는 판정 제외, "
                  f"측정이상 추정 신호만 산출: {sorted(_meas_only)}")

    # ── PCHK 종류별 '검증 대상 CAT2' 매핑 (ANOMALY_KNOWLEDGE.md에서 관리) ──
    #   예) PCHK_LKG → [VTH, ...](CAT2 이름) : PCHK_LKG가 이 CAT2에 속한 항목들과 동일 PGM(pt)·shot
    #       에서 함께 spec-out일 때만 측정이상으로 본다. PCHK_RES는 다른 CAT2군.
    #   매핑 토큰은 CAT2 이름 기준으로 해석(해당 카테고리 내 항목 전체 검사). CAT2/항목명은 원 이름·표시명
    #   둘 다 인식(_name_forms). 매핑에 없는 PCHK는 모든 spec-out 항목과 대조(하위호환).
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
        """PCHK의 검증 대상 = 매핑에 적힌 'CAT2 이름'들에 속한 ITEM(실제 alias) 집합.

        매핑 토큰은 **CAT2 이름** 기준으로 해석 → 해당 카테고리에 속한 항목 전부를 대상으로 한다.
        (하위호환: 토큰이 항목명 자체와 일치해도 인정). 매핑 없으면 None(전체 대조).
        """
        toks = None
        _pf = _name_forms(pchk_alias)
        for k, v in pchk_item_map.items():
            if _name_forms(k) & _pf:
                toks = v
                break
        if toks is None:
            return None, None
        _tok_forms = set()          # 토큰(=CAT2 이름 목록)의 인식 형태 집합
        for t in toks:
            _tok_forms |= _name_forms(t)
        allowed = set()
        for d in device_items:
            _dcat = cat2_map.get(d, '')
            # (1) 항목의 CAT2가 토큰(CAT2명)과 일치 → 그 카테고리 전체를 검사 대상에 포함
            if _dcat and (_name_forms(_dcat) & _tok_forms):
                allowed.add(d)
            # (2) 하위호환: 토큰이 항목명 자체와 일치해도 인정
            elif _name_forms(d) & _tok_forms:
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
                    # 이 shot(=행)의 PGM(pt) — 겹친 항목 전부 이 값과 동일(같은 행이므로).
                    # Duplicate_Count 기본 접미사('_1'/'_1.0')는 표기에서 제거(중복>1은 유지).
                    _pgm = (re.sub(r'_1(?:\.0+)?$', '', str(tgt.at[idx, col_pgm]))
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
            # base ALIAS 키(예 'MAWIN')로 파생 컬럼(MAWIN_*)까지 매칭 — Trend 차트와 동일 규칙
            _spec = trend_agg_spec(_it, _agg_map)
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
    _item_ctx = {}   # 규칙 평가용 항목별 컨텍스트 {level, disp, tmed, pmed, pspread, tmed_pctile, spec_out_pt, seq}
    _item_stats = {} # AI 해석용 항목별 통계 요약(전 항목) — item_stats_out으로 반환

    def _seq_metrics(it, lo, hi):
        """항목의 wafer별 측정순서(chip_y,chip_x) spec-out 시퀀스 집계 지표(seq_* 규칙 함수용).
        반환 {run: 최대 연속 spec-out 길이(전 wafer 최댓값), dead: 최대 spec-out 비율,
              front: 앞 절반 spec-out 비율 최댓값, back: 뒤 절반 spec-out 비율 최솟값}."""
        _m = {'run': 0, 'dead': 0.0, 'front': 0.0, 'back': 1.0}
        if (lo is None and hi is None) or it not in tgt.columns or not (col_x and col_y and col_waf):
            return _m
        if not (col_x in tgt.columns and col_y in tgt.columns and col_waf in tgt.columns):
            return _m
        _sub = tgt[[col_waf, col_x, col_y, it]].dropna(subset=[it])
        if len(_sub) == 0:
            return _m
        _v = pd.to_numeric(_sub[it], errors='coerce')
        _out = pd.Series(False, index=_sub.index)
        if lo is not None: _out = _out | (_v < lo)
        if hi is not None: _out = _out | (_v > hi)
        _sub = _sub.assign(_out=_out.values)
        _back_min, _any = 1.0, False
        for _w, _wr in _sub.groupby(col_waf):
            if len(_wr) < 5:
                continue
            _any = True
            _ord = _wr.sort_values([col_y, col_x], kind='stable')['_out'].astype(bool).tolist()
            _n = len(_ord); _no = sum(1 for f in _ord if f)
            _run = _cur = 0
            for f in _ord:
                _cur = _cur + 1 if f else 0
                if _cur > _run:
                    _run = _cur
            _m['run'] = max(_m['run'], _run)
            _m['dead'] = max(_m['dead'], _no / _n if _n else 0.0)
            _k = max(1, _n // 2)
            _m['front'] = max(_m['front'], sum(1 for f in _ord[:_k] if f) / _k)
            _back_min = min(_back_min, sum(1 for f in _ord[_k:] if f) / max(1, _n - _k))
        _m['back'] = _back_min if _any else 1.0
        return _m

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
        _wstats = {}    # wafer별 {median, std, n} — 이상/주의 항목의 wafer 통계(요청: findings·AI·룰에 포함)
        if tgt_it is not None:
            for w, g in tgt_it.groupby(col_waf):
                _s = pd.to_numeric(g[it], errors='coerce').dropna()
                if len(_s) == 0:
                    continue
                _wi = _waf_int(w); _wkey = _wi if _wi is not None else w
                _wstats[_wkey] = {'median': float(_s.median()),
                                  'std': float(_s.std()) if len(_s) > 1 else 0.0,
                                  'n': int(len(_s))}
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
        so_pgms, so_zones, so_positions = [], {}, []
        so_pattern, so_pattern_stats, so_commonality = '', {}, ''
        if tgt_it is not None and (lo is not None or hi is not None):
            specout_txt, n_out, specout_map, so_max_ratio, so_n_wafers = \
                _specout_by_wafer(tgt_it, col_waf, it, lo, hi)
            if n_out > 0:
                (so_pgms, so_zones, so_positions, so_pattern,
                 so_pattern_stats, so_commonality) = _specout_extra(it, lo, hi)
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
        # wafer간 반복성(동일 shot/유사 위치) 코멘트 — 3개 wafer 이상 발생 시(요청사항)
        if so_commonality:
            _bits.append(so_commonality)
        # radius zone 분포(위치: Center N ...)는 표시하지 않음(요청). so_zones는 basis에만 기록.
        # median 이탈(dev_txt)은 판정 기준에서 제외됨 → 상세에도 표시하지 않음. 산포(disp_txt)만.
        if disp_txt:
            _bits.append(disp_txt)
        detail = '. '.join(_bits)

        # 판단 severity 결정 — 모든 Index 동일 기준(PCHK 특수처리 없음).
        #   이상(CRITICAL): spec(LCL/UCL) 이탈 point가 하나라도 있으면 이상. (median 기준 미사용)
        #   주의(WARNING) : spec 이내지만 '해당 wafer의 내부 산포'가 다른 wafer(보통 wafer) 대비 큰 경우.
        #   그 외         : 참고(INFO).
        if n_out > 0:
            # 제외 키워드 PCHK는 이상(CRITICAL) 판정 대신 '측정이상 추정(NOTICE)' 신호만
            _sev = 'NOTICE' if it in _meas_only else 'CRITICAL'
        elif worst_disp_ratio > disp_ratio:
            _sev = 'INFO' if it in _meas_only else 'WARNING'
        else:
            _sev = 'INFO'

        # ── 지식 규칙 평가용 항목 컨텍스트 (severity level / 산포배수 / target·pop median) ──
        _tmed = None
        if tgt_it is not None:
            try:
                _tmed = float(pd.to_numeric(tgt_it[it], errors='coerce').median())
            except Exception:
                _tmed = None
        # target median의 모집단 내 백분위(%) — median_pctile() 규칙 원자·AI 통계 요약용
        _tmed_pct = None
        if _tmed is not None and it in pop.columns:
            try:
                _pv = pd.to_numeric(pop[it], errors='coerce').dropna()
                if len(_pv) > 0:
                    _tmed_pct = float((_pv < _tmed).mean() * 100.0)
            except Exception:
                _tmed_pct = None
        # ── wafer별 median/std 대표값(룰 median/stddev 조건·AI·PPT용) ──
        #   rep_std = wafer별 std의 중앙값(대표 산포), rep_median = wafer별 median의 중앙값(대표 중심).
        _rep_std = (float(pd.Series([v['std'] for v in _wstats.values()]).median())
                    if _wstats else None)
        _rep_med = (float(pd.Series([v['median'] for v in _wstats.values()]).median())
                    if _wstats else _tmed)
        _item_ctx[it] = {
            'level': 2 if _sev == 'CRITICAL' else (1 if _sev == 'WARNING' else 0),
            'disp': float(worst_disp_ratio) if worst_disp_ratio else 0.0,
            'tmed': _tmed, 'pmed': pop_med, 'pspread': pop_spread,
            'tmed_pctile': _tmed_pct,
            # median/stddev 룰 조건용 — rep_median/rep_std, spec 경계(median > spec_high*0.9 등)
            'rep_std': _rep_std, 'rep_median': _rep_med,
            'spec_low': lo, 'spec_high': hi,
            'wafer_stats': _wstats,
            'spec_out_pt': int(n_out),           # spec_out(n)/spec_out_pt(ITEM) 규칙 함수용
            'seq': _seq_metrics(it, lo, hi),     # seq_out/seq_front_heavy/seq_mostly_dead 규칙 함수용
            # ── 확장 조건 원자용(전부 이 루프에서 이미 계산된 값 — 추가 비용 없음) ──
            'so_wafers': int(so_n_wafers),       # spec_out_wafers(ITEM): spec-out wafer 수
            'so_ratio': float(so_max_ratio),     # spec_out_ratio(ITEM): wafer 최고 이탈 비율(0~1)
            'med_dev': float(worst_med_dev),     # median_dev_sigma(ITEM): worst wafer median 이탈 σ
            'pattern': so_pattern or '',         # pattern(ITEM, 라벨): 특이맵 라벨(판정 on일 때만)
            'zones': dict(so_zones or {}),       # zone_share(ITEM, Edge): spec-out zone 분포
            'commonality': so_commonality or '', # repeat_shot/repeat_similar(ITEM)
            'ov_shots': int(ov_shots),           # meas_overlap(PCHK): 동일 shot 겹침 수
            'measured': tgt_it is not None,      # measured(ITEM): target lot 측정 존재
        }

        # ── AI 해석용 항목별 통계 요약(전 항목 — finding 유무 무관) ──
        #   "target lot의 wafer 기준 통계가 전체 분포에서 어디쯤인지"를 간단히 요약.
        def _sig4(v):
            try:
                return float(f'{float(v):.4g}')
            except (TypeError, ValueError):
                return None
        _item_stats[it] = {
            'display_name': _disp(it),
            'cat2': cat2_map.get(it, ''),
            'severity': _SEV_LABEL.get(_sev, _sev),
            'spec_out_pt': int(n_out),
            'target_median': _sig4(_tmed),
            'pop_median': _sig4(pop_med),
            'median_pctile': round(_tmed_pct, 1) if _tmed_pct is not None else None,
            'worst_wafer_median_sigma': round(worst_med_dev, 1) if worst_med_dev else 0.0,
            'worst_wafer_dispersion_ratio': round(worst_disp_ratio, 1) if worst_disp_ratio else 0.0,
            'pattern': so_pattern,
            # wafer별 median/std (AI 해석 입력 — 더 정확한 판단). rep_*는 룰/요약 대표값.
            'rep_stddev': _sig4(_rep_std),
            'rep_median': _sig4(_rep_med),
            'wafer_median_std': {str(k): {'median': _sig4(v['median']), 'std': _sig4(v['std']), 'n': v['n']}
                                 for k, v in sorted(_wstats.items(), key=lambda z: (z[0] is None, z[0]))},
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
            'spec_out_positions': so_positions,     # 이상 pt 위치 [{wafer,x,y,pgm}, ...] (상한 적용)
            'spec_out_pattern': so_pattern,         # 특이맵 라벨(Edge ring/줄성/k시 방향 등)
            'spec_out_pattern_stats': so_pattern_stats,   # 패턴 판정 수치/게이트 사유
            'spec_out_commonality': so_commonality, # wafer간 반복성(동일 shot/유사 위치) 코멘트
            'target_median': _tmed,
            'target_median_pctile': round(_tmed_pct, 1) if _tmed_pct is not None else None,
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
        #   display_name/cat2/위치/PGM(pt)/PCHK겹침은 AI 해석 입력용 부가정보(렌더러는 미사용).
        if n_out > 0:
            _extra = {
                'display_name': _disp(it),
                'cat2': cat2_map.get(it, ''),
                'spec_out_pgm': so_pgms,
                'spec_out_zone': so_zones,
                'spec_out_pattern': so_pattern,
                'spec_out_commonality': so_commonality,
                'spec_out_positions': so_positions,
                # 이상 항목의 wafer별 median/std (PPT 상세·AI 입력)
                'wafer_stats': dict(_wstats),
                'rep_stddev': _rep_std, 'rep_median': _rep_med,
            }
            if is_pchk:
                _extra.update({
                    'is_pchk': True,
                    'meas_overlap_shot_count': int(ov_shots),
                    'meas_overlap_items': ov_items,
                    'meas_overlap_examples': [
                        {'wafer': w, 'x': x, 'y': y, 'pgm': pgm, 'items': c}
                        for (w, x, y, pgm, c) in ov_examples],
                })
            if it in _meas_only:
                # 판정 제외 PCHK → 측정이상 추정(NOTICE) 신호. HTML 요약 건수(이상/주의) 미집계,
                # 우선순위 최하(참고) — AI가 겹침 wafer·좌표·PGM(pt)로 측정이상을 추정하는 입력.
                _ov_txt = (f"동일 shot 겹침 {ov_shots}건: "
                           + ', '.join(f"{_disp(k)}({c})" for k, c in
                                       sorted(ov_items.items(), key=lambda z: -z[1]))
                           if ov_shots else "동일 shot 겹침 없음")
                findings.append(_finding(
                    "NOTICE", "MEAS_SUSPECT", it,
                    f"측정이상 추정 신호: {_disp(it)}",
                    (detail.strip() + '. ' if detail.strip() else '') + _ov_txt, **_extra))
                continue
            findings.append(_finding(
                "CRITICAL", "SPEC_OUT", it,
                f"Spec-out: {_disp(it)}", detail.strip(), **_extra))
            continue

        if it in _meas_only:   # spec-out 없는 판정 제외 PCHK → finding 없음
            continue

        if worst_disp_ratio > disp_ratio:
            # 형식: "산포 확대 : ITEM - #W 산포 X배" (제목에 다 담고 상세는 비움 → 깔끔)
            findings.append(_finding(
                "WARNING", "DISPERSION", it,
                f"산포 확대 : {_disp(it)} - #{worst_disp_w} 산포 {worst_disp_ratio:.1f}배", "",
                display_name=_disp(it), cat2=cat2_map.get(it, ''),
                wafer_stats=dict(_wstats), rep_stddev=_rep_std, rep_median=_rep_med))

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
            _row['spec_out_positions'] = '; '.join(
                (f"#{p['wafer']}({p['x']},{p['y']})@{p.get('pgm', '')}" if 'wafer' in p
                 else str(p.get('note', '')))
                for p in _b.get('spec_out_positions', []))
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
    #   md의 [RULE] 체이닝 규칙(단일 포맷)을 파싱해 항목별 통계 판정
    #   (_item_ctx: 이상/주의 level·산포배수·median)과 매칭한다.
    try:
        _chain = _parse_chain_rules(knowledge_text)    # 통합 [RULE] 체이닝 규칙(단일 포맷)
        if _chain:
            _mlow_sigma = cfg('anomaly_median_low_sigma', 2.0)

            _LV = {'이상': 2, '주의': 1, '참고': 0}
            _LVL_KW = {'critical': 2, '이상': 2, 'warning': 1, '주의': 1, '참고': 0, 'info': 0}

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

            def _cat2_ctx(cat2_name):
                """CAT2 그룹의 대표 컨텍스트 = 그 CAT2에 속한 항목들의 '최대 level·최대 disp'.
                해당 CAT2에 항목이 하나도 없으면 None."""
                _nf = _name_forms(cat2_name)
                _lv, _dsp, _found = 0, 0.0, False
                for _cit, _cx in _item_ctx.items():
                    _c2 = cat2_map.get(_cit, '')
                    if _c2 and (_name_forms(_c2) & _nf):
                        _found = True
                        _lv = max(_lv, _cx.get('level', 0))
                        _dsp = max(_dsp, _cx.get('disp', 0.0) or 0.0)
                return {'level': _lv, 'disp': _dsp} if _found else None

            def _eval_atom(atom):
                atom = atom.strip()
                # ── CAT2 그룹 기반 원자 (항목 원자보다 먼저 — 접미 _cat2로 구분) ──
                # sev_cat2(CAT2) 연산자 등급 : 그 CAT2의 최대 item level과 비교
                m = re.match(r'sev_cat2\(([^)]+)\)\s*(>=|<=|==|<|>)\s*(이상|주의|참고)$', atom)
                if m:
                    cc = _cat2_ctx(m.group(1).strip())
                    lv = cc['level'] if cc else 0
                    need = _LV[m.group(3)]; op = m.group(2)
                    return {'>=': lv >= need, '<=': lv <= need, '==': lv == need,
                            '<': lv < need, '>': lv > need}[op]
                # all_sev_cat2(A,B,C)>=이상 : 나열 CAT2 '모두' 해당 등급 이상(각 CAT2에 그 등급 항목 존재)
                m = re.match(r'all_sev_cat2\(([^)]+)\)\s*>=\s*(이상|주의)$', atom)
                if m:
                    need = 2 if m.group(2) == '이상' else 1
                    _ns = [t.strip() for t in m.group(1).split(',') if t.strip()]
                    _cs = [_cat2_ctx(n) for n in _ns]
                    return bool(_cs) and all(c and c['level'] >= need for c in _cs)
                # disp_desc_cat2(A,B,C)/disp_asc_cat2 : CAT2별 최대 산포배수가 순서대로 감소/증가
                m = re.match(r'disp_(desc|asc)_cat2\(([^)]+)\)$', atom)
                if m:
                    _ns = [t.strip() for t in m.group(2).split(',') if t.strip()]
                    _vs = []
                    for n in _ns:
                        c = _cat2_ctx(n)
                        if not c:
                            return False
                        _vs.append(c['disp'])
                    if len(_vs) < 2:
                        return False
                    if m.group(1) == 'desc':
                        return all(_vs[i] > _vs[i + 1] for i in range(len(_vs) - 1))
                    return all(_vs[i] < _vs[i + 1] for i in range(len(_vs) - 1))
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
                # median_pctile(ITEM) <= 5 : target median이 모집단 분포의 하위 5% 이내
                #   (>=95면 상위 5% 이내. 연산자 >= <= < > 지원)
                m = re.match(r'median_pctile\(([^)]+)\)\s*(>=|<=|<|>)\s*([\d.]+)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    v = c.get('tmed_pctile') if c else None
                    if v is None:
                        return False
                    t = float(m.group(3))
                    return {'>=': v >= t, '<=': v <= t, '<': v < t, '>': v > t}[m.group(2)]

                # ── 확장 원자 — 항목 수치/위치/패턴/겹침 (전부 _item_ctx의 기계산 값 참조) ──
                def _cmp(v, op, t):
                    try:
                        v = float(v); t = float(t)
                    except (TypeError, ValueError):
                        return False
                    return {'>=': v >= t, '<=': v <= t, '==': v == t, '<': v < t, '>': v > t}[op]

                # 수치 비교형: spec_out_pt/spec_out_wafers/spec_out_ratio/disp/median_dev_sigma/meas_overlap
                m = re.match(r'(spec_out_pt|spec_out_wafers|spec_out_ratio|disp|median_dev_sigma|meas_overlap)'
                             r'\(([^()]+)\)\s*(>=|<=|==|<|>)\s*([\d.]+)$', atom)
                if m:
                    _key = {'spec_out_pt': 'spec_out_pt', 'spec_out_wafers': 'so_wafers',
                            'spec_out_ratio': 'so_ratio', 'disp': 'disp',
                            'median_dev_sigma': 'med_dev', 'meas_overlap': 'ov_shots'}[m.group(1)]
                    c = _find_ctx(m.group(2).strip())
                    return bool(c) and _cmp(c.get(_key, 0), m.group(3), m.group(4))
                # disp_cat2(CAT2) >= x : CAT2 최대 산포배수 직접 비교
                m = re.match(r'disp_cat2\(([^()]+)\)\s*(>=|<=|==|<|>)\s*([\d.]+)$', atom)
                if m:
                    cc = _cat2_ctx(m.group(1).strip())
                    return bool(cc) and _cmp(cc.get('disp', 0), m.group(2), m.group(3))
                # median_high(ITEM) : target median이 제품 대비 매우 높음(median_low 대칭, 임계 σ 동일)
                m = re.match(r'median_high\(([^()]+)\)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    if not c or c['tmed'] is None or c['pmed'] is None or not c['pspread']:
                        return False
                    return (c['tmed'] - c['pmed']) / c['pspread'] >= _mlow_sigma
                # pattern(ITEM, 라벨) : 특이맵 라벨 부분일치(대소문자 무시 — 특이맵 판정 on일 때만 의미)
                m = re.match(r'pattern\(([^,()]+),\s*([^()]+)\)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    return bool(c) and m.group(2).strip().lower() in str(c.get('pattern', '')).lower()
                # zone_share(ITEM, Edge|Middle|Center) >= f : spec-out 중 해당 zone 비율(0~1)
                m = re.match(r'zone_share\(([^,()]+),\s*([A-Za-z가-힣]+)\s*\)\s*(>=|<=|<|>)\s*([\d.]+)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    _zs = (c or {}).get('zones') or {}
                    _tot = sum(_zs.values())
                    _zn = m.group(2).strip().lower()
                    _v = next((v for k, v in _zs.items() if str(k).lower() == _zn), 0)
                    return _tot > 0 and _cmp(_v / _tot, m.group(3), m.group(4))
                # repeat_shot/repeat_similar(ITEM) : wafer간 동일 shot/유사 위치 반복 코멘트 존재
                m = re.match(r'repeat_(shot|similar)\(([^()]+)\)$', atom)
                if m:
                    c = _find_ctx(m.group(2).strip())
                    _cm = str((c or {}).get('commonality', ''))
                    return ('동일 shot' in _cm) if m.group(1) == 'shot' else ('유사 위치' in _cm)
                # measured(ITEM) : 항목이 target lot에서 측정됨
                m = re.match(r'measured\(([^()]+)\)$', atom)
                if m:
                    c = _find_ctx(m.group(1).strip())
                    return bool(c and c.get('measured'))
                # count_sev(critical) >= n : 해당 등급 이상인 항목 개수(전 항목 대상)
                m = re.match(r'count_sev\((critical|warning|이상|주의)\)\s*(>=|<=|==|<|>)\s*([\d.]+)$', atom)
                if m:
                    _need = _LVL_KW[m.group(1).lower()]
                    _n = sum(1 for _cx in _item_ctx.values() if _cx.get('level', 0) >= _need)
                    return _cmp(_n, m.group(2), m.group(3))
                return False

            def _eval_when(expr):
                for _grp in re.split(r'\s+OR\s+', expr):
                    _atoms = [a for a in re.split(r'\s+AND\s+', _grp) if a.strip()]
                    if _atoms and all(_eval_atom(a) for a in _atoms):
                        return True
                return False

            # ── 통합 [RULE] 체이닝용 원자 평가 (측정순서 함수·spec_out·all_sev(그룹,level) 등) ──
            def _eval_chain_atom(atom, tctx):
                atom = atom.strip()
                _sq = (tctx or {}).get('seq', {})
                # spec_out <op> n : trigger의 spec-out pt 수
                m = re.match(r'spec_out\s*(>=|<=|==|<|>)\s*([\d.]+)$', atom)
                if m:
                    v = (tctx or {}).get('spec_out_pt', 0); t = float(m.group(2)); op = m.group(1)
                    return {'>=': v >= t, '<=': v <= t, '==': v == t, '<': v < t, '>': v > t}[op]
                # seq_out(n) : trigger 측정순서 최대 연속 spec-out ≥ n
                m = re.match(r'seq_out\(\s*([\d.]+)\s*\)$', atom)
                if m:
                    return float(_sq.get('run', 0)) >= float(m.group(1))
                # seq_mostly_dead(frac) : trigger 측정순서 spec-out 비율 ≥ frac
                m = re.match(r'seq_mostly_dead\(\s*([\d.]+)\s*\)$', atom)
                if m:
                    return float(_sq.get('dead', 0.0)) >= float(m.group(1))
                # seq_front_heavy : 앞 절반 이탈 많고(≥0.6) 뒤 절반 양호(≤0.2)
                if atom == 'seq_front_heavy':
                    return float(_sq.get('front', 0.0)) >= 0.6 and float(_sq.get('back', 1.0)) <= 0.2
                # all_sev(그룹..., level) : 나열 그룹(CAT2 또는 항목) '모두' ≥ level
                m = re.match(r'all_sev\(([^)]+)\)$', atom)
                if m:
                    _args = [t.strip() for t in m.group(1).split(',') if t.strip()]
                    if len(_args) >= 2 and _args[-1].lower() in _LVL_KW:
                        need = _LVL_KW[_args[-1].lower()]
                        for g in _args[:-1]:
                            cc = _cat2_ctx(g) or _find_ctx(g)
                            if not cc or cc.get('level', 0) < need:
                                return False
                        return True
                # sev(ITEM, level) : 항목 등급(신규 시그니처)
                m = re.match(r'sev\(([^,]+),\s*(critical|warning|이상|주의|참고|info)\)$', atom)
                if m:
                    cc = _find_ctx(m.group(1).strip())
                    return bool(cc) and cc.get('level', 0) >= _LVL_KW[m.group(2).lower()]

                # ── wafer별 median/stddev 조건 (trigger 항목 기준) ──
                def _ccmp(v, op, t):
                    try:
                        v = float(v); t = float(t)
                    except (TypeError, ValueError):
                        return False
                    return {'>=': v >= t, '<=': v <= t, '==': v == t, '<': v < t, '>': v > t}[op]

                def _rhs(expr):
                    """비교 우변 → 숫자. spec_high/spec_low[*계수] 지원(예: spec_high*0.9)."""
                    expr = expr.strip()
                    mm = re.match(r'(spec_high|spec_low)\s*(?:\*\s*([\d.]+))?$', expr)
                    if mm:
                        base = (tctx or {}).get(mm.group(1))
                        if base is None:
                            return None
                        return float(base) * (float(mm.group(2)) if mm.group(2) else 1.0)
                    try:
                        return float(expr)
                    except (TypeError, ValueError):
                        return None

                # sev <op> N : trigger 항목 등급(0참고/1주의/2이상) 숫자 비교 (예: sev >= 1)
                m = re.match(r'sev\s*(>=|<=|==|<|>)\s*([\d.]+)$', atom)
                if m:
                    return _ccmp((tctx or {}).get('level', 0), m.group(1), m.group(2))
                # stddev(=std) <op> RHS : trigger 대표 산포(wafer std 중앙값) 비교 (예: stddev < 0.5)
                m = re.match(r'(?:stddev|std)\s*(>=|<=|==|<|>)\s*(.+)$', atom)
                if m:
                    v = (tctx or {}).get('rep_std'); t = _rhs(m.group(2))
                    return v is not None and t is not None and _ccmp(v, m.group(1), t)
                # median <op> RHS : trigger 대표 중심(wafer median 중앙값) 비교 (예: median > spec_high*0.9)
                #   (median_low/high/pctile(...)은 뒤에 '(' 또는 '_'가 붙어 여기 매칭 안 됨 → 기존 원자로 감)
                m = re.match(r'median\s*(>=|<=|==|<|>)\s*(.+)$', atom)
                if m:
                    v = (tctx or {}).get('rep_median'); t = _rhs(m.group(2))
                    return v is not None and t is not None and _ccmp(v, m.group(1), t)

                # 그 외는 기존 원자 평가로 폴백 (sev()연산자·disp_desc/asc·median_*·*_cat2 등)
                return _eval_atom(atom)

            def _eval_chain_cond(expr, tctx):
                if not expr:
                    return True
                for _grp in re.split(r'\s+OR\s+', expr):
                    _atoms = [a for a in re.split(r'\s+AND\s+', _grp) if a.strip()]
                    if _atoms and all(_eval_chain_atom(a, tctx) for a in _atoms):
                        return True
                return False

            # ── 통합 [RULE] 규칙 평가(단일 포맷) ──
            #   각 [RULE]은 gate(when) 통과 후:
            #   (a) suppress_disp: 나열 항목의 산포(주의) finding 억제
            #   (b) compare_disp : 두 그룹 산포 비교 finding 생성
            #   (c) 분기(when2→when3…): '먼저 만족한' 분기의 note/link 하나만 [불량 모드] finding.
            _suppress_disp_items = set()   # DISPERSION(주의) finding을 억제할 항목(실제 키)
            for _cr in _chain:
                try:
                    _tname = _cr.get('trigger', '')
                    _rname = _cr.get('name') or (f"[RULE] {_tname}" if _tname else '[RULE]')
                    _gate = _cr.get('when') or ''
                    _tctx = _find_ctx(_tname) if _tname else None
                    _gate_ok = (not _gate) or _eval_chain_cond(_gate, _tctx)
                    # (a) 산포 억제 액션 — 게이트 참(또는 게이트 없음)일 때 적용
                    if _cr.get('suppress_disp'):
                        if _gate_ok:
                            for _n in _cr['suppress_disp']:
                                _ri = _resolve_item(_n)
                                if _ri:
                                    _suppress_disp_items.add(_ri)
                        _rule_trace.append({'kind': 'RULE(산포억제)', 'name': _rname,
                                            'cond': f"when: {_gate}" if _gate else '(항상)',
                                            'matched': bool(_gate_ok),
                                            'result': '적용' if _gate_ok else '게이트 미충족',
                                            'note': ('산포 언급 억제: ' + ', '.join(_cr['suppress_disp'])) if _gate_ok else ''})
                        if not (_cr.get('compare_disp') or _cr.get('branches')):
                            continue
                    # (b) 산포 그룹 비교 액션 — 게이트 참일 때 비교 finding 생성
                    if _cr.get('compare_disp'):
                        _cmp = ''
                        if _gate_ok:
                            _g1, _g2 = _cr['compare_disp']
                            _v1, _v2 = _grp_disp(_g1), _grp_disp(_g2)
                            # 표시명 후처리(suffix/prefix 제거) 적용해 그룹 항목명 표기
                            _g1d = ', '.join(_disp(_x) for _x in _g1)
                            _g2d = ', '.join(_disp(_x) for _x in _g2)
                            _big = f"[{_g1d}]" if _v1 >= _v2 else f"[{_g2d}]"
                            _cmp = f"산포 비교: [{_g1d}]={_v1:.1f}배 vs [{_g2d}]={_v2:.1f}배 → {_big} 산포가 더 큼"
                            _lvl = ('CRITICAL' if str(_cr.get('sev', 'warning')).lower()
                                    in ('critical', '이상') else 'WARNING')
                            findings.append(_finding(_lvl, 'KNOWLEDGE', _rname,
                                                     f"[지식 판정] {_rname}", _cmp))
                        _rule_trace.append({'kind': 'RULE(산포비교)', 'name': _rname,
                                            'cond': f"when: {_gate}" if _gate else '(항상)',
                                            'matched': bool(_gate_ok),
                                            'result': '매칭' if _gate_ok else '게이트 미충족',
                                            'note': _cmp})
                        if not _cr.get('branches'):
                            continue
                    # (c) 분기 판정 — 게이트 통과 후 when2→when3… 순으로 최초 매칭 1개만
                    if not _cr.get('branches'):
                        continue
                    if not _gate_ok:
                        _rule_trace.append({'kind': 'RULE(체이닝)', 'name': _rname,
                                            'cond': f"when: {_gate}", 'matched': False,
                                            'result': '게이트 미충족', 'note': ''})
                        continue   # 게이트 미충족 → 규칙 스킵
                    _hit_note = None; _hit_idx = None; _hit_link = ''
                    for _bi, _br in enumerate(_cr.get('branches', [])):
                        _cond = _br.get('cond')
                        if _cond is None or _eval_chain_cond(_cond, _tctx):
                            _note = (_br.get('note') or '').strip()
                            if not _note:
                                break   # 코멘트 없는 분기 → finding 없이 종료(첫 매칭만)
                            _lvl = ('CRITICAL' if str(_cr.get('sev', 'critical')).lower()
                                    in ('critical', '이상') else 'WARNING')
                            _det = f"참고: {_br['link']}" if _br.get('link') else ''
                            _tk = _resolve_item(_tname) if _tname else None
                            findings.append(_finding(
                                _lvl, 'DEFECT_MODE', _tname or _note,
                                f"[불량 모드] {_note}", _det,
                                cat2=cat2_map.get(_tk, '') if _tk else '',
                                display_name=_disp(_tname) if _tname else ''))
                            _hit_note = _note; _hit_idx = _bi + 1; _hit_link = _br.get('link') or ''
                            break   # 최초 매칭 분기 1개만
                    # trigger의 median/std 실측값 — rule check 이력에 남겨 판정 근거를 투명하게
                    def _statnote(_c):
                        if not _c:
                            return ''
                        _rm = _c.get('rep_median'); _rs = _c.get('rep_std')
                        _p = []
                        if _rm is not None:
                            _p.append(f"median={float(_rm):.4g}")
                        if _rs is not None:
                            _p.append(f"stddev={float(_rs):.4g}")
                        return ('[' + ', '.join(_p) + ']') if _p else ''
                    _sn = _statnote(_tctx)
                    if _hit_note:
                        _bcond = _cr.get('branches', [])[_hit_idx - 1].get('cond')
                        _rule_trace.append({'kind': 'RULE(체이닝)', 'name': _rname,
                                            'cond': (f"when:{_gate} → " + (f"분기{_hit_idx}({_bcond})" if _bcond else '기본(else)')),
                                            'matched': True,
                                            'result': f"매칭 → 분기{_hit_idx}: [불량 모드] {_hit_note}",
                                            'note': (_sn + (' ' if _sn else '') + (f"참고: {_hit_link}" if _hit_link else '')).strip()})
                    else:
                        _rule_trace.append({'kind': 'RULE(체이닝)', 'name': _rname,
                                            'cond': f"when:{_gate} (게이트 통과)", 'matched': False,
                                            'result': '게이트 통과·분기 무매칭', 'note': _sn})
                except Exception:
                    continue
            # 억제 적용: 지정 항목의 DISPERSION(주의) finding 제거 (spec-out=이상은 유지)
            if _suppress_disp_items:
                findings[:] = [f for f in findings
                               if not (f.get('type') == 'DISPERSION' and f.get('item') in _suppress_disp_items)]
    except Exception as _ke:
        print(f"[WARN] 지식 규칙/불량모드 평가 실패: {_ke}")

    # ── 전체 rule 체크 결과 터미널 출력(진행 상황 + 요약) ──
    try:
        _n_all = len(_rule_trace)
        _n_hit = sum(1 for _t in _rule_trace if _t.get('matched'))
        if _n_all:
            print(f"[RULE CHECK] anomaly rule {_n_all}개 체크 — 매칭 {_n_hit}, 해당없음 {_n_all - _n_hit}")
            for _t in _rule_trace:
                _mk = 'O' if _t.get('matched') else '.'
                print(f"  [{_mk}] {_t.get('kind','')} · {_t.get('name','')} → {_t.get('result','')}")
        else:
            print("[RULE CHECK] 정의된 anomaly rule 없음(체크 대상 0개)")
    except Exception:
        pass
    # 호출자(Main)로 전체 rule 체크 결과 반환 — RUN/AI 파일 저장·PPT 반영에 사용
    if isinstance(rule_trace_out, list):
        rule_trace_out.extend(_rule_trace)

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
        if t == 'DEFECT_MODE':   # 최종 불량 모드(decision tree) — 종합 결론 → 최상단
            return 40000.0 + (100.0 if f.get('severity') == 'CRITICAL' else 0.0)
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
    # 항목별 통계 요약을 호출자(→ interpret_with_ai의 [항목 통계])로 반환
    if isinstance(item_stats_out, dict):
        item_stats_out.update(_item_stats)
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
                      config=None, target_lot_id="", item_stats=None):
    """AI 다단계 해석: 각 단계의 판단을 다음 단계 입력으로 넘겨 최종 판단 생성.

    단계
    ----
    1) Triage   : 코드 Finding을 CAT2/현상(phenomenon) 단위로 묶고 중요도 정렬
    2) RootCause: 지식베이스(knowledge_text)를 참고해 각 현상의 추정 원인 도출
    3) Final    : 1·2를 종합해 최종 판단을 **구조화 JSON**으로 산출 → 코드가
                  '불량 모드 판정표'(DEFECT_MODE_TABLE)와 대조 **검증** 후 HTML 조립.
                  defect_mode가 표에 없으면 '미매칭(수동 검토)' 처리, LINK/COMMENT는
                  표의 값만 첨부(LLM이 만든 링크는 무시 — 할루시네이션 차단. LINK는 선택).

    Parameters
    ----------
    findings : list[dict]   analyze_commonality() 결과
    metrics_dict : dict     항목별 통계(참고용)
    knowledge_text : str     ANOMALY_KNOWLEDGE.md 내용(통계 패턴→원인)
    llm_fn : callable        llm_fn(system: str, user: str) -> str. 없으면 None 반환(코드만 사용)
    config, target_lot_id : 메타
    item_stats : dict|None   analyze_commonality(item_stats_out=)가 채운 항목별 통계 요약
                             — [항목 통계] 블록으로 Triage/Final에 전달

    참고: RUN/EXAMPLE/*.md 가 있으면 '판정 예시(few-shot)'로 Final 프롬프트에 포함
    (없어도 동작. 파일명 '_' 시작은 템플릿으로 간주하고 스킵. 작성법은 README 참조).

    Returns
    -------
    str | None : [0] 섹션에 곁들일 AI 해석 HTML. 실패/미사용 시 None(→ 코드 분석만 표시)
    """
    if llm_fn is None or not findings:
        return None
    import json

    def _slim(f):
        """AI에 전달할 finding 필드. 기본(severity~detail) 외에
        display_name(후처리 표시명)·cat2(Triage 그룹핑 기준)·
        spec_out_pgm/zone/positions(이상 pt의 PGM(pt)·위치)·
        meas_overlap_*(PCHK 동일 shot 겹침 — 측정이상 추정 근거)를 있으면 포함."""
        keys = ("severity", "type", "item", "display_name", "cat2", "title", "detail",
                "spec_out_pgm", "spec_out_zone", "spec_out_pattern", "spec_out_commonality",
                "spec_out_positions", "is_pchk", "meas_overlap_shot_count",
                "meas_overlap_items", "meas_overlap_examples")
        return {k: f.get(k) for k in keys if f.get(k) not in (None, '', [], {})}
    fjson = json.dumps([_slim(f) for f in findings], ensure_ascii=False)
    # spec-out으로 분류된 Index 조합 (불량 모드 판정 입력) — "원이름(표시명)" 표기
    def _iname(f):
        _i, _d = f.get('item'), f.get('display_name')
        return f"{_i}({_d})" if _d and _d != _i else str(_i)
    spec_items = [_iname(f) for f in findings if f.get('type') == 'SPEC_OUT']

    # 항목별 통계 요약([항목 통계] 블록) — 전 분석 항목의 wafer 기준 위치/산포 요약
    _stats_block = ''
    if item_stats:
        try:
            _stats_block = ("\n\n[항목 통계] (전 항목 — severity, target median과 모집단 내 "
                            "백분위 median_pctile(%), worst wafer median σ·산포배수, 특이맵 패턴)\n"
                            + json.dumps(item_stats, ensure_ascii=False, default=str))
        except Exception:
            _stats_block = ''

    # few-shot 판정 예시(RUN/EXAMPLE/*.md, 선택) — 과거 '입력→확정 판정' 사례
    def _load_examples():
        try:
            import os, glob
            _dir = (getattr(config, 'ai_examples_dir', None) if config else None) \
                or os.path.join('RUN', 'EXAMPLE')
            if not os.path.isdir(_dir):
                return ''
            _maxn = int(getattr(config, 'ai_examples_max', 5) or 5) if config else 5
            _maxc = int(getattr(config, 'ai_examples_max_chars', 6000) or 6000) if config else 6000
            parts, total = [], 0
            for fp in sorted(glob.glob(os.path.join(_dir, '*.md'))):
                if os.path.basename(fp).startswith('_'):   # _TEMPLATE 등 스킵
                    continue
                if len(parts) >= _maxn:
                    break
                try:
                    with open(fp, encoding='utf-8') as _ef:
                        txt = _ef.read().strip()
                except Exception:
                    continue
                if not txt or total + len(txt) > _maxc:
                    continue
                parts.append(f"--- 예시: {os.path.basename(fp)} ---\n{txt}")
                total += len(txt)
            return '\n\n'.join(parts)
        except Exception:
            return ''
    _examples = _load_examples()
    _examples_block = (
        "\n\n[판정 예시] 과거 실제 사례(관찰 입력 → 후행 확인된 판정)입니다. 입력이 유사하면 "
        "같은 판정을 우선 적용하세요. 예시가 판정표와 충돌하면 예시(실사례)가 우선합니다.\n"
        + _examples) if _examples else ''

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

    # ── 호출 모드: 'multi'(기본, Triage→Root-cause→Final 3회) / 'single'(Final 1회) ──
    #   single은 비용/지연 1/3, 단계간 오류 전파 없음. 모델이 약해 그룹핑 품질이 떨어지면 multi.
    _mode = (str(getattr(config, 'ai_stage_mode', 'multi') or 'multi').lower()
             if config else 'multi')
    try:
        triage = rootcause = ''
        if _mode != 'single':
            # ── 1단계: Triage / 현상 그룹핑 ──
            triage = _stage(
                "① Triage",
                "당신은 반도체 TEG 데이터 분석가입니다. 코드가 산출한 이상 finding 목록을 "
                "현상(phenomenon) 단위로 묶고 중요도 순으로 3~6개로 정리하세요. "
                "각 finding에는 item(원 이름)·display_name(후처리 표시명)·cat2(항목 카테고리)가 있습니다. "
                "**그룹핑 기준: cat2가 같은 항목은 하나의 현상으로 묶고**, 현상 머리에 `[CAT2]`를 표기하세요 "
                "(cat2가 없는 항목은 항목명 유사성으로 묶음). 항목명은 display_name(표시명)으로 적되 "
                "원 이름이 다르면 괄호로 병기하세요. "
                "PCHK(is_pchk=true, 측정 의심)가 있으면 최상단에 별도 표기하고, "
                "meas_overlap_examples·spec_out_positions의 wafer·좌표·PGM(pt)를 근거로 "
                "**어느 wafer/PGM(pt)에서 어떤 항목과 동일 shot으로 겹치는지** 명시하세요. "
                "각 현상에는 wafer별 이상 pt 수(detail)와 함께 위치 요약(특이맵 패턴 spec_out_pattern, "
                "spec_out_zone: Center/Middle/Edge 분포, PGM(pt) 목록)을 덧붙이고, "
                "wafer간 반복 코멘트(spec_out_commonality — 동일 shot 반복/유사 위치 반복)가 있으면 "
                "**그대로 인용**하세요(반복 위치는 systematic 가능성 신호). "
                "[항목 통계]가 주어지면 target median의 모집단 내 백분위(median_pctile)·산포배수 등 "
                "특기할 수치를 현상 서술에 활용하세요. 간결한 불릿으로. "
                "**주어진 finding·[항목 통계]에 적힌 사실(항목·spec-out·이탈 수치·위치·패턴·PGM(pt))만 사용**하고, "
                "원인·해석·반도체 공정 지식을 추가하지 마세요(요약/그룹핑만).",
                f"대상 lot: {target_lot_id}\n[findings]\n{fjson}{_stats_block}")

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

        # ── 최종 판단 + 불량 모드 판정 — 구조화 JSON 출력 → 코드 검증·HTML 조립 ──
        #   spec-out Index 조합을 [지식베이스]의 '불량 모드 판정표'와 대조하여 판정.
        #   여러 모드가 동시 매칭되면 표에서 더 위(번호 작은) 모드를 택한다.
        #   LINK/COMMENT는 LLM이 아니라 코드가 판정표에서 첨부(할루시네이션 차단, LINK는 선택).
        _final_sys = (
            "당신은 책임 엔지니어입니다. 아래 현상/근거를 종합해 최종 판단을 내리되, "
            "**[지식베이스]와 관찰된 데이터(finding·항목 통계)에 있는 내용만** 사용하세요. "
            "**[지식베이스]에 적혀있지 않은 반도체 공정 지식·원인·조치(예: '산화막 두께', "
            "'식각 균일성' 등 md에 없는 도메인 지식)를 임의로 판단하거나 추가하지 마세요.** "
            "[지식베이스]의 '판정 규칙'(ANOMALY_RULES 마커 사이의 [RULE] 블록들)을 이용해 "
            "spec-out Index 조합으로부터 불량 모드를 판정하세요(각 [RULE] **분기의 note 문구가 곧 불량 모드명**). "
            "**[RULE]에 정의된 note 문구 외의 불량 모드명을 새로 만들지 마세요 — 매칭되는 규칙이 없으면 "
            "defect_mode는 반드시 null**로 출력합니다(임의 모드명 생성 금지). "
            "Index명은 원 이름과 표시명 어느 쪽으로 적혀 있어도 같은 항목으로 인식하고, [RULE]의 "
            "CAT2 조건은 finding의 cat2와 대조합니다. 규칙은 위에서부터 우선순위가 높고 여러 모드가 동시 매칭되면 "
            "**가장 위(먼저 정의된)** 모드 하나로 판정합니다(한 [RULE] 안에서는 먼저 만족한 분기 하나). "
            "측정 의심(PCHK 동일 shot 겹침)이 있으면 [지식베이스]의 '측정이상 추정 규칙'을 적용해 "
            "겹친 wafer·좌표·PGM(pt)를 명시하고 불량 단정 전 재측정 권고를 우선하며, "
            "해당 site를 제외한 나머지 spec-out만으로 불량 모드를 판정하세요. "
            "항목명은 표시명(display_name) 기준으로 서술합니다. "
            "**출력은 아래 형식의 JSON 객체 하나만**(코드펜스·설명문·HTML 금지): "
            '{"defect_mode": "<매칭된 [RULE] 분기의 note 문구(=불량 모드) 그대로. 매칭 없으면 null>", '
            '"basis_items": ["<근거가 된 Index 표시명>", ...], '
            '"summary": "<종합 판단 1~2문장 — finding·지식베이스에 근거한 판단만>", '
            '"phenomenon": "<핵심 현상 — 관찰된 사실만. 어느 Index가 어느 wafer/특이맵 패턴·PGM(pt)에서 어떻게 spec-out/이탈했는지. 원인 추측 금지>", '
            '"actions": "<권고 조치 — 지식베이스·RULE의 NOTE에 명시된 것만. 없으면 \'지식베이스 미기재\'>", '
            '"meas_suspect": "<측정이상 추정 서술(겹친 wafer·좌표·PGM(pt) 명시) 또는 null>"} '
            "URL/링크는 출력하지 마세요 — 코드가 매칭 RULE의 LINK를 자동 첨부합니다(LINK 없는 모드도 있음)."
            f"{_examples_block}\n"
            f"[지식베이스]\n{knowledge_text or '(지식베이스 없음)'}")
        _spec_line = f"[spec-out Index 조합]\n{', '.join(spec_items) if spec_items else '(없음)'}"
        if _mode == 'single':
            _final_sys += ("\n(단일 호출 모드: [현상]/[근거] 대신 [findings] JSON이 직접 주어집니다. "
                           "cat2가 같은 finding을 스스로 현상 단위로 묶어 판단하고, PCHK(is_pchk)가 "
                           "있으면 meas_overlap_*로 측정이상 추정을 먼저 검토하세요.)")
            _final_user = (f"대상 lot: {target_lot_id}\n[findings]\n{fjson}\n\n"
                           f"{_spec_line}{_stats_block}")
            final = _stage("① Final(단일 호출)", _final_sys, _final_user)
        else:
            _final_user = f"{_spec_line}\n\n[현상]\n{triage}\n\n[근거]\n{rootcause}{_stats_block}"
            final = _stage("③ Final", _final_sys, _final_user)

        # 응답이 JSON 객체가 아니면 같은 입력으로 1회 재시도(형식 지시 강화) — 그래도
        # 실패하면 _assemble_final_html이 텍스트/HTML 폴백으로 처리(하위호환).
        if _extract_json_obj(final) is None:
            final = _stage(
                "Final(JSON 재시도)",
                "직전 응답이 JSON 객체 형식이 아니었습니다. 다른 텍스트/코드펜스 없이 "
                "반드시 JSON 객체 하나만 출력하세요.\n" + _final_sys, _final_user)

        body = _assemble_final_html(final, _parse_defect_modes(knowledge_text))
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
