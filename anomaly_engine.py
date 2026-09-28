# -*- coding: utf-8 -*-
"""
anomaly_engine - 코드 기반 Commonality / 이상 해석 엔진
======================================================

ET 측정 데이터에서 이상 항목을 감지하고, 1차 자동 해석(Finding)을 코드만으로 산출합니다.
외부 LLM 연결 없이 동작합니다(순수 통계 판정).

주요 기능:
    - analyze_commonality(): 각 Index 항목별 '한 개'의 이상 Finding 산출
        · spec-out(CRITICAL) → 이탈 개수/최대 이탈값
        · spec 미초과 시 Flier/wafer 산포 확대/수준 이동/trend/SPC run 중 하나 (WARNING)
        · detector profile과 임계값은 My_config 또는 제품별 config.yaml에서 선택
    - render_findings_html(): Finding 리스트 → HTML(<ul>, severity별 색상)

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
import warnings
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt

# Matplotlib 백엔드를 Agg(비-GUI)로 설정하여 서버 환경 호환
matplotlib.use('Agg')
plt.rcParams['axes.unicode_minus'] = False   # U+2212 글리프 없는 폰트 경고 방지
# 렌더 텍스트에 실제 U+2212가 섞이면 unicode_minus=False만으로는 "Glyph ... missing
# from font(s)" 경고가 계속 나므로 메시지 패턴으로 무음 처리(워커 프로세스 포함).
warnings.filterwarnings("ignore", message=".*Glyph.*")
# "findfont: Font family 'NanumGothic' not found." — logging 채널(font_manager 로거)이라
# warnings 필터로는 안 잡힘. 로거 레벨 상향으로 무음 처리(렌더링 동작에는 영향 없음).
import logging as _mpl_logging
_mpl_logging.getLogger('matplotlib.font_manager').setLevel(_mpl_logging.ERROR)


# ===================================================================
#  코드 기반 Commonality / 이상 해석 엔진 (AI 없이 동작)
# ===================================================================

# ──────────────────────────────────────────────────────────────────────
# Finding severity 등급 — 신호등 3색 (HTML/PPT 색상·정렬에 사용)
#   CRITICAL : 빨강 ● 이상            (spec-out 불량모드 등 확정적 이상)
#   WARNING  : 주황 ● 주의            (spec 이내 이상 신호 — 플라이어(소수 pt 이탈)/산포 확대)
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
    """trend_tkout_agg 매칭 → 집계 스펙 문자열('PXX' 임의 백분위수/'MEDIAN'/'MEAN') 또는 None.

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


def detect_series_signals(history_values, target_values, timeline_values,
                          enabled_detectors=None, settings=None):
    """측정 대표값 시계열에서 수준 이동·지속 추세·SPC 연속 이상을 검출한다.

    입력은 raw site가 아니라 호출부가 (lot, wafer, tkout)별 대표값으로 축약한 1차원 값이다.
    history_values는 target 이전 baseline, target_values는 현재 리포트 lot, timeline_values는
    target 시점까지 시간순 전체 대표값이다. 외부 ML 라이브러리 없이 NumPy/Pandas만 사용해
    사내 이식 환경에서도 동일하게 동작한다.

    반환: [{'type','score','title','basis','stats'}, ...]
      - LEVEL_SHIFT : target cluster가 baseline 중심에서 통째로 이동
      - TREND       : 최근 window에서 robust 직선 기울기가 지속
      - SPC_RUN     : 같은 쪽 연속/2-of-3/4-of-5 규칙
    """
    import numpy as np

    enabled = {str(x).strip().lower() for x in (enabled_detectors or [])}
    cfg = dict(settings or {})

    def _clean(values):
        s = pd.to_numeric(pd.Series(values), errors='coerce').dropna().astype(float)
        return s[np.isfinite(s.values)].to_numpy(dtype=float)

    def _robust_np(values):
        a = _clean(values)
        if len(a) == 0:
            return None, None
        med = float(np.median(a))
        spread = float(1.4826 * np.median(np.abs(a - med)))
        if not np.isfinite(spread) or spread <= 0:
            q25, q75 = np.quantile(a, [0.25, 0.75]) if len(a) > 1 else (med, med)
            spread = float((q75 - q25) / 1.349) if q75 > q25 else 0.0
        if (not np.isfinite(spread) or spread <= 0) and len(a) > 1:
            spread = float(np.std(a, ddof=1))
        return med, (spread if np.isfinite(spread) and spread > 0 else None)

    history = _clean(history_values)
    target = _clean(target_values)
    timeline = _clean(timeline_values)
    min_baseline = max(3, int(cfg.get('min_baseline', 20) or 20))
    min_target = max(1, int(cfg.get('min_target', 3) or 3))
    center, spread = _robust_np(history)
    if len(history) < min_baseline or center is None or not spread:
        return []

    signals = []

    # ① 수준 이동: target 중앙값 이탈 크기 + target 점들의 방향 일치율.
    if 'level_shift' in enabled and len(target) >= min_target:
        threshold = float(cfg.get('level_shift_sigma', 3.0) or 3.0)
        min_fraction = float(cfg.get('level_shift_min_fraction', 0.75) or 0.75)
        target_med = float(np.median(target))
        delta = target_med - center
        dev_sigma = abs(delta) / spread
        direction = 1.0 if delta >= 0 else -1.0
        same_fraction = float(np.mean((target - center) * direction > 0))
        if dev_sigma >= threshold and same_fraction >= min_fraction:
            side = '상향' if direction > 0 else '하향'
            signals.append({
                'type': 'LEVEL_SHIFT',
                'score': float(dev_sigma / max(threshold, 1e-12)),
                'title': f'수준 이동({side}) {dev_sigma:.1f}σ',
                'criterion': (f'중앙값 이탈≥{threshold:g}σ & 같은 방향 비율≥{min_fraction:.0%}'),
                'basis': (f'target median이 과거 baseline 대비 {side} {dev_sigma:.1f}σ, '
                          f'같은 방향 측정점 {same_fraction:.0%}'),
                'stats': {'deviation_sigma': round(dev_sigma, 3),
                          'same_direction_fraction': round(same_fraction, 4),
                          'baseline_center': center, 'baseline_spread': spread,
                          'target_median': target_med},
            })

    # ② robust trend: 모든 pair slope의 중앙값(Theil-Sen 핵심 아이디어)으로 단발점 영향을 억제.
    if 'trend' in enabled:
        window = max(5, int(cfg.get('trend_window', 12) or 12))
        y = timeline[-window:]
        if len(y) >= min(window, 6):
            x = np.arange(len(y), dtype=float)
            slopes = [(y[j] - y[i]) / (j - i)
                      for i in range(len(y) - 1) for j in range(i + 1, len(y))]
            slope = float(np.median(slopes)) if slopes else 0.0
            intercept = float(np.median(y - slope * x))
            pred = intercept + slope * x
            ss_tot = float(np.sum((y - np.mean(y)) ** 2))
            ss_res = float(np.sum((y - pred) ** 2))
            r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
            change_sigma = abs(slope) * max(1, len(y) - 1) / spread
            diffs = np.diff(y)
            nz = diffs[np.abs(diffs) > max(spread * 1e-6, 1e-15)]
            if len(nz):
                direction_fraction = float(max(np.mean(nz > 0), np.mean(nz < 0)))
            else:
                direction_fraction = 0.0
            min_change = float(cfg.get('trend_total_sigma', 3.0) or 3.0)
            min_r2 = float(cfg.get('trend_min_r2', 0.60) or 0.60)
            min_dir = float(cfg.get('trend_min_direction_fraction', 0.65) or 0.65)
            if change_sigma >= min_change and r2 >= min_r2 and direction_fraction >= min_dir:
                side = '상승' if slope > 0 else '하락'
                signals.append({
                    'type': 'TREND',
                    'score': float(change_sigma / max(min_change, 1e-12)),
                    'title': f'지속 {side} trend {change_sigma:.1f}σ',
                    'criterion': (f'최근 {len(y)}점 총 변화≥{min_change:g}σ & '
                                  f'R²≥{min_r2:g} & 같은 방향 변화≥{min_dir:.0%}'),
                    'basis': (f'최근 {len(y)}점 robust slope 기준 총 변화 {change_sigma:.1f}σ, '
                              f'R²={r2:.2f}, 같은 방향 변화 {direction_fraction:.0%}'),
                    'stats': {'window': int(len(y)), 'slope': slope,
                              'total_change_sigma': round(change_sigma, 3),
                              'r2': round(r2, 4),
                              'direction_fraction': round(direction_fraction, 4)},
                })

    # ③ SPC run: Western Electric 계열의 지속/반복 신호. baseline은 target 이전 이력만 사용.
    if 'spc_run' in enabled and len(timeline) >= 3:
        z = (timeline - center) / spread
        hits = []
        same_n = max(3, int(cfg.get('spc_same_side_points', 8) or 8))
        same_last = float(cfg.get('spc_same_side_min_last_sigma', 0.8) or 0.8)
        if len(z) >= same_n:
            tail = z[-same_n:]
            if (np.all(tail > 0) or np.all(tail < 0)) and abs(float(tail[-1])) >= same_last:
                hits.append(f'같은 쪽 {same_n}점 연속')
        two_sigma = float(cfg.get('spc_two_of_three_sigma', 2.0) or 2.0)
        if len(z) >= 3:
            tail = z[-3:]
            if max(int(np.sum(tail > two_sigma)), int(np.sum(tail < -two_sigma))) >= 2:
                hits.append(f'3점 중 2점 {two_sigma:g}σ 초과')
        four_sigma = float(cfg.get('spc_four_of_five_sigma', 1.0) or 1.0)
        if len(z) >= 5:
            tail = z[-5:]
            if max(int(np.sum(tail > four_sigma)), int(np.sum(tail < -four_sigma))) >= 4:
                hits.append(f'5점 중 4점 {four_sigma:g}σ 초과')
        if hits:
            max_tail = float(np.max(np.abs(z[-max(same_n, 5):])))
            signals.append({
                'type': 'SPC_RUN',
                'score': max(1.0, max_tail / 3.0),
                'title': 'SPC 연속 이상',
                'criterion': (f'같은 쪽 {same_n}점 / 3점 중 2점>{two_sigma:g}σ / '
                              f'5점 중 4점>{four_sigma:g}σ'),
                'basis': ', '.join(hits) + f' (최근 최대 {max_tail:.1f}σ)',
                'stats': {'rules': hits, 'max_tail_sigma': round(max_tail, 3)},
            })

    return sorted(signals, key=lambda x: float(x.get('score', 0.0)), reverse=True)


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
#     (anomaly_basis_<lot>_<step_id>.json의 spec_out_pattern_stats — "왜 이 특이맵인지" 근거).
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


def analyze_commonality(merged_df, target_lot_id, metrics_dict, spec_data,
                        main_vehicle=None, config=None, reformatter=None,
                        knowledge_text="", item_stats_out=None, rule_trace_out=None,
                        json_rules=None, report_key=None, persist_basis=True):
    """코드 기반 다중 detector로 이상 Finding 리스트를 산출(순수 통계, 외부 LLM·룰 없음).

    각 detector는 독립적으로 try/except 처리되어 하나가 실패해도 나머지 분석은 계속됩니다.

    Parameters
    ----------
    merged_df : pd.DataFrame   전체(모든 lot, vehicle+with_vehicle) 피벗 데이터
    target_lot_id : str        리포팅 대상 fab_lot_id
    metrics_dict : dict        insert_plots가 계산한 항목별 통계
    spec_data : pd.DataFrame    ALIAS 인덱스, SPECLOW/SPECHIGH 보유
    main_vehicle : str          모집단 기준 vehicle 명(없으면 전체 사용)
    config : object             임계값(My_config). 없으면 기본값
    item_stats_out : dict|None  전달 시 항목별 통계 요약({item: {...}})을 채워 반환
    knowledge_text / rule_trace_out / json_rules : 과거 호환용으로만 유지하며 무시한다.
    report_key : str|None       산출물 파일 키 "{lot}_{step_id}"(원본 step_id) — anomaly_basis
                                파일명에 사용. None이면 target_lot_id만(단독 테스트용).

    Returns
    -------
    list[dict] : Finding 목록 (severity 순 정렬). 각 항목
        {severity, type, item, title, detail}
    """
    def cfg(k, d):
        """vehicle YAML → generated/default Config 순으로 조회."""
        if config is None:
            return d
        try:
            if hasattr(config, 'get'):
                return config.get(k, d)
        except Exception:
            pass
        return getattr(config, k, d)

    # detector profile 해석. anomaly_enabled_detectors(list)가 있으면 profile보다 우선한다.
    _profile = str(cfg('anomaly_detector_profile', 'legacy') or 'legacy').strip().lower()
    _profiles = cfg('anomaly_detector_profiles', {}) or {}
    _enabled_raw = cfg('anomaly_enabled_detectors', None)
    if _enabled_raw is None:
        _enabled_raw = _profiles.get(_profile) or ['spec_out', 'flier', 'dispersion']
    if isinstance(_enabled_raw, str):
        _enabled_raw = [x.strip() for x in _enabled_raw.split(',') if x.strip()]
    _enabled_detectors = {str(x).strip().lower() for x in (_enabled_raw or [])}
    _sensitive = _profile == 'sensitive'

    def _profile_value(base_key, default):
        """sensitive profile이면 *_sensitive 값을 우선, 없으면 일반 값을 사용."""
        if _sensitive:
            _sv = cfg(base_key + '_sensitive', None)
            if _sv is not None:
                return _sv
        return cfg(base_key, default)

    _series_settings = {
        'min_baseline': cfg('anomaly_series_min_baseline', 20),
        'min_target': cfg('anomaly_series_min_target', 3),
        'level_shift_sigma': _profile_value('anomaly_level_shift_sigma', 3.0),
        'level_shift_min_fraction': _profile_value('anomaly_level_shift_min_fraction', 0.75),
        'trend_window': cfg('anomaly_trend_window', 12),
        'trend_total_sigma': _profile_value('anomaly_trend_total_sigma', 3.0),
        'trend_min_r2': _profile_value('anomaly_trend_min_r2', 0.60),
        'trend_min_direction_fraction': _profile_value('anomaly_trend_min_direction_fraction', 0.65),
        'spc_same_side_points': _profile_value('anomaly_spc_same_side_points', 8),
        'spc_same_side_min_last_sigma': _profile_value('anomaly_spc_same_side_min_last_sigma', 0.8),
        'spc_two_of_three_sigma': _profile_value('anomaly_spc_two_of_three_sigma', 2.0),
        'spc_four_of_five_sigma': _profile_value('anomaly_spc_four_of_five_sigma', 1.0),
    }

    disp_ratio = cfg('anomaly_lot_dispersion_ratio', 1.5)
    # ── 주의(WARNING) 세부 판정 설정 (My_config — HTML/PPT 안내문에도 동일 값이 동적 표기됨) ──
    flier_sigma = float(cfg('anomaly_flier_sigma', 3.5) or 0)         # Flier 임계 σ (0=OFF)
    flier_max_pts = int(cfg('anomaly_flier_max_pts', 0) or 0)         # Flier 최대 pt 상한(0=상한 없음)
    flier_offdir_relax = float(cfg('anomaly_flier_offdir_relax', 2.0) or 1.0)  # 반대 방향 완화 배수(UPPER/LOWER)
    disp_min_spec_frac = float(cfg('anomaly_disp_min_spec_frac', 0.0) or 0.0)  # 산포 절대량 게이트(spec 폭 비율)

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
    col_rad = _pick_col(merged_df, 'Chip_Radius', 'chip_radius')   # Data Extractor radius(mm)

    # metrics_dict 기반 항목 + spec_data에 REPORT ORDER가 있고 merged_df에 컬럼이 존재하는 항목 보강
    # (PPT에서 skip된 항목도 anomaly 감지 대상에 포함)
    _items_from_metrics = [it for it in metrics_dict.keys() if it in merged_df.columns]
    _items_from_spec = []
    if spec_data is not None:
        for _si in spec_data.index:
            if _si in merged_df.columns and _si not in metrics_dict:
                _items_from_spec.append(_si)
    items = _items_from_metrics + _items_from_spec
    items = list(dict.fromkeys(items))   # 순서 유지 중복 제거
    if _items_from_spec:
        print(f"[anomaly] metrics_dict 미포함 항목 {len(_items_from_spec)}개 보강: {_items_from_spec[:5]}{'...' if len(_items_from_spec) > 5 else ''}")

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

    # direction map {item: 'UPPER'|'LOWER'|'BOTH'} — REPORT DIRECTION 기반 flier 방향 감시용
    direction_map = {}
    try:
        if spec_data is not None and 'REPORT DIRECTION' in getattr(spec_data, 'columns', []):
            for _idx in spec_data.index:
                _dv = spec_data.loc[_idx, 'REPORT DIRECTION']
                _dv = str(_dv).strip().upper() if pd.notna(_dv) else 'BOTH'
                direction_map[_idx] = _dv if _dv in ('UPPER', 'LOWER', 'BOTH') else 'BOTH'
    except Exception as e:
        print(f"[anomaly] REPORT DIRECTION 파싱 실패: {e}")

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
                # PCHK 인식 = CAT2가 'PCHK'이거나 ALIAS에 'PCHK' 포함(부분일치 —
                # Main.py의 pchk_keep 컬럼 보존 규칙과 동일 기준. 예: RMAX_PCHK_LKG도 인식)
                cat2 = str(r.get('CAT2', '')).upper()
                if (cat2 == 'PCHK' or 'PCHK' in str(a).upper()) \
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
    _agg_label_map = {}   # {item: 'P10'/'MEDIAN'/... } — 요약 근거 문구용(agg 기준 표기)
    try:
        import numpy as _np
        _agg_map = cfg('trend_tkout_agg', {}) or {}

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
                    try:
                        _agg_label_map[_ai] = str(trend_agg_spec(_ai, _agg_map) or '').strip().upper()
                    except Exception:
                        _agg_label_map[_ai] = ''
    except Exception as _ae:
        print(f"[WARN] trend_tkout_agg 판정용 집계 실패: {_ae}")

    def _series_representatives(frame, it, until=None):
        """(lot, wafer, tkout)별 median 대표값을 시간순 1차원 시계열로 반환."""
        if frame is None or len(frame) == 0 or it not in frame.columns:
            return []
        _cols = [c for c in [col_lot, col_waf, col_time, it] if c and c in frame.columns]
        if it not in _cols:
            return []
        _d = frame[_cols].copy()
        _d[it] = pd.to_numeric(_d[it], errors='coerce')
        _d = _d.dropna(subset=[it])
        if until is not None and col_time and col_time in _d.columns:
            _d[col_time] = pd.to_datetime(_d[col_time], errors='coerce')
            _d = _d[_d[col_time] <= until]
        if len(_d) == 0:
            return []
        _gk = [c for c in [col_lot, col_waf, col_time] if c and c in _d.columns]
        if _gk:
            _s = _d.groupby(_gk, dropna=False)[it].median().reset_index()
            if col_time and col_time in _s.columns:
                _s[col_time] = pd.to_datetime(_s[col_time], errors='coerce')
                _s = _s.sort_values(col_time, kind='stable')
            return pd.to_numeric(_s[it], errors='coerce').dropna().tolist()
        return pd.to_numeric(_d[it], errors='coerce').dropna().tolist()

    _basis = []      # 판단 근거 중간 데이터 (RUN/TEMP 저장용) — 전 Index 통합
    _rankinfo = {}   # 항목별 정렬 지표 (spec-out 비율/wafer 수/이탈 크기/REPORT ORDER)
    _item_ctx = {}   # 규칙 평가용 항목별 컨텍스트 {level, disp, tmed, pmed, pspread, tmed_pctile, spec_out_pt, seq}
    _item_stats = {} # AI 해석용 항목별 통계 요약(전 항목) — item_stats_out으로 반환

    def _seq_metrics(it, lo, hi):
        """항목의 wafer별 측정순서 spec-out 시퀀스 집계 지표(seq_* 규칙 함수용).

        측정순서 = (chip_y_adj, chip_x_adj) 오름차순 정렬 → chip_x_adj가 먼저 증가:
        (1,1)→(2,1)→(3,1)→… — WF MAP 기준 좌상단부터 한 줄씩 우측으로 진행하는
        실제 측정(터치다운) 순서와 동일하다.
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
        disp_txt = ''
        flier_txt = ''
        worst_med_dev, worst_med_w, worst_med_val = 0.0, None, None
        worst_disp_ratio, worst_disp_w = 0.0, None
        worst_flier_w, worst_flier_cnt, worst_flier_dev = None, 0, 0.0
        worst_flier_observed, worst_flier_limit = 0.0, 0.0
        _wstats = {}    # wafer별 {median, std, n} — 이상/주의 항목의 wafer 통계(요청: findings·AI·룰에 포함)
        _rws = []       # wafer별 robust 산포(1.4826×MAD) — rstd/rstd_asc 룰 원자용
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
                if ws:
                    _rws.append(float(ws))
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
                # ── Flier: wafer median 대비 '보통 wafer 산포'의 flier_sigma σ 초과 pt ──
                #   1개 이상이면 Flier 주의(flier_max_pts>0이면 그 개수 이하일 때만 — 초과는 산포 쪽).
                #   MAD 기반 산포배수는 소수 pt에 둔감해 이 케이스를 못 잡으므로 상보적 판정.
                if typ_wspread and flier_sigma > 0 and len(_s) >= 5:
                    _smed = float(_s.median())
                    _raw_dev = (_s - _smed) / typ_wspread  # 부호 있는 편차(양수=위, 음수=아래)
                    _item_dir = direction_map.get(it, 'BOTH')

                    if _item_dir == 'UPPER':
                        # 상한 감시: 위쪽(spec 방향) flier는 정상 감도, 아래쪽(반대)은 flier_offdir_relax배 완화
                        _fmask = (_raw_dev > flier_sigma) | (_raw_dev < -(flier_sigma * flier_offdir_relax))
                    elif _item_dir == 'LOWER':
                        # 하한 감시: 아래쪽(spec 방향) flier는 정상 감도, 위쪽(반대)은 flier_offdir_relax배 완화
                        _fmask = (_raw_dev < -flier_sigma) | (_raw_dev > (flier_sigma * flier_offdir_relax))
                    else:  # BOTH — 방향 개념 없음: 양방향 동일 감도(완화 미적용)
                        _fmask = _raw_dev.abs() > flier_sigma

                    _fcnt = int(_fmask.sum())
                    _fdev_abs = _raw_dev.abs()

                    # 우선순위는 기존대로 spec 방향 최대 편차를 사용한다. 표시 근거는 실제로
                    # 임계를 넘긴 pt의 관측값/적용 기준을 별도 보존해 사람이 바로 비교하게 한다.
                    if _item_dir == 'UPPER':
                        _spec_dir_dev = _raw_dev[_raw_dev > 0]
                        _fdev_max = float(_spec_dir_dev.max()) if len(_spec_dir_dev) > 0 else float(_fdev_abs.max())
                    elif _item_dir == 'LOWER':
                        _spec_dir_dev = -_raw_dev[_raw_dev < 0]
                        _fdev_max = float(_spec_dir_dev.max()) if len(_spec_dir_dev) > 0 else float(_fdev_abs.max())
                    else:
                        _fdev_max = float(_fdev_abs.max())

                    _fired = _raw_dev[_fmask]
                    _f_observed = float(_fired.abs().max()) if len(_fired) else 0.0
                    _fired_peak = float(_fired.loc[_fired.abs().idxmax()]) if len(_fired) else 0.0
                    if _item_dir == 'UPPER':
                        _f_limit = flier_sigma if _fired_peak > 0 else flier_sigma * flier_offdir_relax
                    elif _item_dir == 'LOWER':
                        _f_limit = flier_sigma if _fired_peak < 0 else flier_sigma * flier_offdir_relax
                    else:
                        _f_limit = flier_sigma

                    if (_fcnt >= 1 and (flier_max_pts <= 0 or _fcnt <= flier_max_pts)
                            and _fdev_max > worst_flier_dev):
                        _wi = _waf_int(w)
                        worst_flier_dev = _fdev_max
                        worst_flier_observed = _f_observed
                        worst_flier_limit = float(_f_limit)
                        worst_flier_w = _wi if _wi is not None else w
                        worst_flier_cnt = _fcnt
            if worst_disp_ratio >= 1.3 and worst_disp_w is not None:
                disp_txt = f"#{worst_disp_w} 산포 {worst_disp_ratio:.1f}배"
            if worst_flier_w is not None:
                flier_txt = f"#{worst_flier_w} Flier {worst_flier_cnt}pt(최대 {worst_flier_dev:.1f}σ)"

        # ── agg(trend_tkout_agg 집계 판정) 항목은 '산포(wafer 내부 분산)' 기준 주의를 띄우지 않는다 ──
        #   agg 항목은 판정 자체가 집계값(P10/MEDIAN 등) 기준이라 raw 측정치의 wafer 내부 산포는
        #   의미가 다르다(집계로 이미 대표값 1개로 축약됨). 산포배수·산포 코멘트를 여기서 무효화해
        #   built-in WARNING(severity)·DISPERSION finding·산포 규칙(disp/disp_ratio atom)·순위/metrics
        #   에서 모두 제외한다. spec-out(CRITICAL) 판정은 agg 집계값 기준으로 그대로 유지된다.
        if it in _agg_item_set:
            worst_disp_ratio, worst_disp_w, disp_txt = 0.0, None, ''
            worst_flier_w, worst_flier_cnt, worst_flier_dev, worst_flier_observed, worst_flier_limit, flier_txt = None, 0, 0.0, 0.0, 0.0, ''
        if 'flier' not in _enabled_detectors:
            worst_flier_w, worst_flier_cnt, worst_flier_dev, worst_flier_observed, worst_flier_limit, flier_txt = None, 0, 0.0, 0.0, 0.0, ''
        if 'dispersion' not in _enabled_detectors:
            worst_disp_ratio, worst_disp_w, disp_txt = 0.0, None, ''

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

        # ── 시계열 모양 detector: 수준 이동 / robust trend / SPC 연속 이상 ──
        # target 이전 제품 이력만 baseline으로 쓰고, target 시점까지의 대표값을 시간순 평가한다.
        _series_signals = []
        if _enabled_detectors.intersection({'level_shift', 'trend', 'spc_run'}):
            try:
                _hist_frame = pop
                if col_lot and col_lot in pop.columns:
                    _hist_frame = pop[pop[col_lot].astype(str) != str(target_lot_id)]
                _target_until = None
                if col_time and col_time in tgt.columns and len(tgt):
                    _target_until = pd.to_datetime(tgt[col_time], errors='coerce').max()
                    if pd.isna(_target_until):
                        _target_until = None
                _hist_values = _series_representatives(_hist_frame, it, until=_target_until)
                _target_values = _series_representatives(tgt, it, until=_target_until)
                _timeline_values = _series_representatives(pop, it, until=_target_until)
                _series_signals = detect_series_signals(
                    _hist_values, _target_values, _timeline_values,
                    enabled_detectors=_enabled_detectors, settings=_series_settings)
            except Exception as _se:
                print(f"[anomaly] 시계열 detector 실패({it}): {_se}")
                _series_signals = []

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
            'worst_flier_dev': float(worst_flier_dev),
            'series_score': max([float(s.get('score', 0.0)) for s in _series_signals] or [0.0]),
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
        # median 이탈은 판정 기준에서 제외됨 → 상세에도 표시하지 않음. 플라이어/산포만.
        if flier_txt:
            _bits.append(flier_txt)
        if disp_txt:
            _bits.append(disp_txt)
        if _series_signals:
            _bits.extend(str(s.get('basis', '')) for s in _series_signals if s.get('basis'))
        detail = '. '.join(_bits)

        # ── 산포 확대 절대량 게이트: worst wafer의 절대 robust 산포(배수×보통 wafer 산포)가
        #    spec 폭(UCL−LCL)의 disp_min_spec_frac 미만이면 산포 주의를 띄우지 않는다
        #    (spec 대비 무의미하게 작은 산포 확대 오탐 억제). 단측 spec/무spec은 게이트 미적용.
        _disp_gate_ok = True
        if (disp_min_spec_frac > 0 and lo is not None and hi is not None and hi > lo
                and typ_wspread and worst_disp_ratio > 0):
            _disp_gate_ok = (worst_disp_ratio * typ_wspread) >= disp_min_spec_frac * (hi - lo)

        # 판단 severity 결정 — 모든 Index 동일 기준(PCHK 특수처리 없음).
        #   이상(CRITICAL): spec(LCL/UCL) 이탈 point가 하나라도 있으면 이상. (median 기준 미사용)
        #   주의(WARNING) : spec 이내지만 ① Flier(wafer median 대비 보통 wafer 산포의
        #                   flier_sigma σ 초과 pt 1개 이상) 또는
        #                   ② 산포 확대(wafer 내부 산포가 보통 wafer 대비 disp_ratio배 초과,
        #                   disp_min_spec_frac>0이면 절대량 게이트도 통과)인 경우.
        #   그 외         : 참고(INFO).
        if n_out > 0 and 'spec_out' in _enabled_detectors:
            # 제외 키워드 PCHK는 이상(CRITICAL) 판정 대신 '측정이상 추정(NOTICE)' 신호만
            _sev = 'NOTICE' if it in _meas_only else 'CRITICAL'
        elif worst_flier_w is not None:
            _sev = 'INFO' if it in _meas_only else 'WARNING'
        elif worst_disp_ratio > disp_ratio and _disp_gate_ok:
            _sev = 'INFO' if it in _meas_only else 'WARNING'
        elif _series_signals:
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
        # ── robust std 대표값 — rstd(ITEM)·rstd_asc/desc(산포 벌어짐 방향) 룰 원자용 ──
        #   rep_rstd   = wafer별 robust 산포(1.4826×MAD)의 중앙값(단위 있음).
        #   rstd_ratio = rep_rstd / 보통 wafer 산포(typ_wspread) — 단위 무관 배수라 '항목 간' 비교 가능.
        _rep_rstd = (float(pd.Series(_rws).median()) if _rws else None)
        _rstd_ratio = (float(_rep_rstd / typ_wspread)
                       if (_rep_rstd is not None and typ_wspread) else 0.0)
        # agg 항목은 robust 산포(rstd)도 '산포 기준'이므로 규칙(rstd/rstd_asc/desc)에서 제외
        #   — disp와 동일 취지(위 worst_disp_ratio 무효화 참조). 배수 원자를 0/None으로.
        if it in _agg_item_set:
            _rep_rstd, _rstd_ratio = None, 0.0
        _item_ctx[it] = {
            'level': 2 if _sev == 'CRITICAL' else (1 if _sev == 'WARNING' else 0),
            'disp': float(worst_disp_ratio) if worst_disp_ratio else 0.0,
            'tmed': _tmed, 'pmed': pop_med, 'pspread': pop_spread,
            'tmed_pctile': _tmed_pct,
            # median/stddev 룰 조건용 — rep_median/rep_std, spec 경계(median > spec_high*0.9 등)
            'rep_std': _rep_std, 'rep_median': _rep_med,
            'rep_rstd': _rep_rstd, 'rstd_ratio': _rstd_ratio,   # rstd()·rstd_asc/desc 원자용
            'spec_low': lo, 'spec_high': hi,
            'wafer_stats': _wstats,
            'spec_out_pt': int(n_out),           # spec_out(n)/spec_out_pt(ITEM) 규칙 함수용
            'seq': _seq_metrics(it, lo, hi),     # seq_out/seq_front_heavy/seq_mostly_dead 규칙 함수용
            # ── 확장 조건 원자용(전부 이 루프에서 이미 계산된 값 — 추가 비용 없음) ──
            'so_wafers': int(so_n_wafers),       # spec_out_wafers(ITEM): spec-out wafer 수
            'so_wafer_ids': sorted(set(w for ws in specout_map.values() for w in ws),
                                   key=lambda z: (z is None, z)),  # spec-out wafer 번호 목록
            'so_ratio': float(so_max_ratio),     # spec_out_ratio(ITEM): wafer 최고 이탈 비율(0~1)
            'med_dev': float(worst_med_dev),     # median_dev_sigma(ITEM): worst wafer median 이탈 σ
            'flier_pt': int(worst_flier_cnt),    # 플라이어 pt 수(worst wafer, 1~max일 때만 >0)
            'flier_dev': float(worst_flier_dev), # 플라이어 최대 이탈 σ(보통 wafer 산포 기준)
            'pattern': so_pattern or '',         # pattern(ITEM, 라벨): 특이맵 라벨(판정 on일 때만)
            'zones': dict(so_zones or {}),       # zone_share(ITEM, Edge): spec-out zone 분포
            'commonality': so_commonality or '', # repeat_shot/repeat_similar(ITEM)
            'ov_shots': int(ov_shots),           # meas_overlap(PCHK): 동일 shot 겹침 수
            'measured': tgt_it is not None,      # measured(ITEM): target lot 측정 존재
            'series_signals': list(_series_signals),  # 수준 이동/trend/SPC detector trace
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
            'flier_pt': int(worst_flier_cnt),
            'flier_max_dev_sigma': round(worst_flier_dev, 1) if worst_flier_dev else 0.0,
            'pattern': so_pattern,
            'detector_profile': _profile,
            'series_signals': list(_series_signals),
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
            'dispersion_abs_gate_pass': bool(_disp_gate_ok),   # 산포 절대량 게이트(spec 폭 비율) 통과 여부
            'flier_wafer': worst_flier_w,                      # 플라이어 worst wafer
            'flier_pt': int(worst_flier_cnt),                  # 플라이어 pt 수(1~flier_max_pts)
            'flier_max_dev_sigma': round(worst_flier_dev, 2) if worst_flier_dev else 0.0,
            'detector_profile': _profile,
            'enabled_detectors': sorted(_enabled_detectors),
            'series_signals': list(_series_signals),
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
        if n_out > 0 and 'spec_out' in _enabled_detectors:
            _extra = {
                'display_name': _disp(it),
                'cat2': cat2_map.get(it, ''),
                'affected_wafer_ids': list(_item_ctx[it].get('so_wafer_ids') or []),
                'spec_out_pgm': so_pgms,
                'spec_out_zone': so_zones,
                'spec_out_pattern': so_pattern,
                'spec_out_commonality': so_commonality,
                'spec_out_positions': so_positions,
                # 이상 항목의 wafer별 median/std (PPT 상세·AI 입력)
                'wafer_stats': dict(_wstats),
                'rep_stddev': _rep_std, 'rep_median': _rep_med,
            }
            # ── 요약용 간결 근거(어느 샷인지 나열 X): 측정값 spec 이탈 / agg 기준 spec 이탈 + 방향 ──
            _bv = pd.to_numeric(tgt_it[it], errors='coerce')
            _b_below = lo is not None and bool((_bv < lo).any())
            _b_above = hi is not None and bool((_bv > hi).any())
            if _b_below and _b_above:
                _b_side = 'spec 범위 이탈'
            elif _b_below:
                _b_side = f'spec 하한({float(lo):.4g}) 미만'
            elif _b_above:
                _b_side = f'spec 상한({float(hi):.4g}) 초과'
            else:
                _b_side = 'spec 이탈'
            _b_count = f"이탈 {n_out}pt" + (f" / {so_n_wafers}개 wafer" if so_n_wafers > 0 else "")
            if it in _agg_item_set:
                _extra['basis'] = (f"{_agg_label_map.get(it) or '집계'} 집계값이 {_b_side}"
                                   f" · {_b_count}")
            else:
                _extra['basis'] = (f"측정값이 {_b_side}"
                                   f" · {_b_count}")
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

        # ── 주의① Flier: spec 이내지만 소수 pt 뜬 케이스 — 산포 확대보다 우선 표기 ──
        if worst_flier_w is not None:
            findings.append(_finding(
                "WARNING", "FLIER", it,
                f"Flier : {_disp(it)} - #{worst_flier_w} {worst_flier_cnt}pt "
                f"(최대 {worst_flier_observed:.1f}σ)", "",
                display_name=_disp(it), cat2=cat2_map.get(it, ''),
                affected_wafer_ids=[worst_flier_w],
                basis=(f"spec 이내 · 관측 최대 이탈 {worst_flier_observed:.1f}σ > "
                       f"기준 {worst_flier_limit:g}σ "
                       f"(wafer median 대비 보통 wafer 산포), 초과 {worst_flier_cnt}pt"),
                wafer_stats=dict(_wstats), rep_stddev=_rep_std, rep_median=_rep_med))
            continue

        # ── 주의② 산포 확대(DISPERSION): 배수 초과 (+게이트 설정 시 절대량 통과) ──
        if worst_disp_ratio > disp_ratio and _disp_gate_ok:
            # 형식: "산포 확대 : ITEM - #W 산포 X배" (제목에 다 담고 상세는 비움 → 깔끔)
            findings.append(_finding(
                "WARNING", "DISPERSION", it,
                f"산포 확대 : {_disp(it)} - #{worst_disp_w} 산포 {worst_disp_ratio:.1f}배", "",
                display_name=_disp(it), cat2=cat2_map.get(it, ''),
                affected_wafer_ids=[worst_disp_w],
                basis=(f"관측 wafer 내부 산포 {worst_disp_ratio:.1f}배 > "
                       f"기준 {disp_ratio:g}배 (보통 wafer 내부 산포 대비)"),
                wafer_stats=dict(_wstats), rep_stddev=_rep_std, rep_median=_rep_med))
            continue

        # ── 주의③ 시계열 모양: 수준 이동 / 지속 trend / SPC 연속 이상 ──
        # 동일 항목에서 여러 detector가 걸려도 finding·차트는 1개만 만들고 trace는 모두 보존한다.
        if _series_signals:
            _primary = _series_signals[0]
            _other = [s.get('title', s.get('type', '')) for s in _series_signals[1:]]
            _trace = (f"관측: {_primary.get('basis', '')} · "
                      f"판정 기준: {_primary.get('criterion', '')}")
            _detail = _trace
            if _other:
                _detail += '. 동시 신호: ' + ', '.join(str(x) for x in _other if x)
            findings.append(_finding(
                "WARNING", str(_primary.get('type', 'SERIES_ANOMALY')), it,
                f"{_primary.get('title', '시계열 이상')} : {_disp(it)}", _detail,
                display_name=_disp(it), cat2=cat2_map.get(it, ''),
                basis=_trace, detector_profile=_profile,
                detector_signals=list(_series_signals),
                wafer_stats=dict(_wstats), rep_stddev=_rep_std, rep_median=_rep_med))

    # ── 판단 근거 중간 데이터를 RUN/TEMP에 저장 (csv + json) ──
    try:
        import os, json
        if not persist_basis:
            raise StopIteration
        _outdir = os.getenv('AUTO_REPORT_TEMP_DIR') or os.path.join('RUN', 'TEMP')
        os.makedirs(_outdir, exist_ok=True)
        _safe_lot = str(report_key or target_lot_id).replace('/', '_').replace('\\', '_')
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
    except StopIteration:
        pass
    except Exception as _be:
        print(f"[WARN] anomaly 근거 데이터 저장 실패: {_be}")

    # NOTE: 지식 규칙([RULE]/NL_RULES)·불량모드 판정은 사용하지 않는다(순수 통계만).

    # ── Priority(우선순위) 명시적 수식 — 값이 클수록 우선(위에 정렬) ──
    #   이상(SPEC_OUT) : P = 20000 + 100·R_max + N_wf/100
    #        R_max = 항목 내 '최대 wafer spec-out 비율' = max_wafer(이탈 pt / 측정 pt), 0~1
    #        N_wf  = spec-out wafer 수 (0~25, 동점 tie-break용으로 /100 축소)
    #   주의(FLIER)     : P = 10000 + 100·(F/kσ)
    #        F = 플라이어 최대 이탈 σ, kσ = anomaly_flier_sigma (임계 대비 배수 — 산포배수 D와 동일 스케일)
    #   주의(DISPERSION): P = 10000 + 100·D
    #        D = 항목 내 '최대 wafer 산포배수' = max_wafer(wafer 내부 robust 산포 / 보통 wafer 산포)
    #   주의(LEVEL_SHIFT/TREND/SPC_RUN): P = 10000 + 100·S
    #        S = detector 임계 대비 score(1.0=임계 통과선, 클수록 강한 신호)
    #   참고(그 외)     : P = 100·D
    #   → 이상(20000+) > 주의(10000+) > 참고(<수백) 순이 항상 보장. 동점 시 REPORT ORDER 오름차순.
    def _priority(f):
        ri = _rankinfo.get(f.get('item', ''), {})
        t = f.get('type')
        if t == 'SPEC_OUT':
            return 20000.0 + 100.0 * ri.get('max_ratio', 0.0) + ri.get('n_so_wafers', 0) / 100.0
        if t == 'FLIER':
            return 10000.0 + 100.0 * (ri.get('worst_flier_dev', 0.0) / flier_sigma
                                      if flier_sigma > 0 else 0.0)
        if t == 'DISPERSION':
            return 10000.0 + 100.0 * ri.get('worst_disp_ratio', 0.0)
        if t in ('LEVEL_SHIFT', 'TREND', 'SPC_RUN'):
            return 10000.0 + 100.0 * ri.get('series_score', 0.0)
        return 100.0 * ri.get('worst_disp_ratio', 0.0)

    for _f in findings:
        _f['priority'] = round(_priority(_f), 3)   # 투명성 위해 finding에 priority 값 부착

    #   동순위 tie-break: REPORT ORDER 오름차순.
    def _tiebreak(f):
        return _rankinfo.get(f.get('item', ''), {}).get('report_order', 1e9)

    findings.sort(key=lambda f: (-_priority(f), _tiebreak(f)))
    # 항목별 통계 요약을 호출자로 반환
    if isinstance(item_stats_out, dict):
        item_stats_out.update(_item_stats)
    return findings


def render_findings_html(findings, top_n=5, detail_ref="PPT의 Score Board 다음 'Anomaly 상세(통계)' 페이지",
                         kind='stat', tail_note='', empty_msg=None):
    """Finding 리스트를 카테고리별 대표 1건으로 줄여 상위 top_n건만 HTML로 렌더링.

    findings는 analyze_commonality에서 severity(우선순위) 순으로 정렬되어 들어온다.
    cat2가 같은 finding은 첫 번째(=가장 높은 우선순위)만 표시한다. cat2가 없으면 서로
    다른 독립 finding으로 보존한다.
    kind='stat'      : 통계 자동 분석(이상/주의 건수 head).
    tail_note        : 목록 아래에 덧붙일 안내(예: 일반 이상 N건은 PPT 상세 참조).
    empty_msg        : findings 없을 때 문구 override.
    """
    if not findings:
        if empty_msg is None:
            empty_msg = '통계 자동 분석 결과 유의미한 이상/commonality 신호 없음.'
        base = ('<ul style="font-size:14px; color:#333; margin:5px 0 8px; padding-left:20px;">'
                f'<li><strong>[요약]</strong> {empty_msg}</li></ul>')
        return base + (tail_note or '')
    n_crit = sum(1 for f in findings if f["severity"] == "CRITICAL")
    n_warn = sum(1 for f in findings if f["severity"] == "WARNING")
    # head: 신호등 범례 겸 건수 (● 이상 N | ● 주의 X).
    _div = ' <span style="color:#bbb;">|</span> '
    head = (f'<div style="font-size:13px; color:#333; margin:4px 0;">'
            f'<b>통계 기반 자동 분석</b>: '
            f'{_sev_dot("CRITICAL")} {_SEV_HEAD["CRITICAL"]} {n_crit}건{_div}'
            f'{_sev_dot("WARNING")} {_SEV_HEAD["WARNING"]} {n_warn}건</div>')
    # 같은 CAT2에서는 이미 우선순위 정렬된 첫 finding 하나만 Anomaly Summary에 표시한다.
    # CAT2가 비어 있는 RULE/기타 finding까지 한 카테고리로 오인하지 않도록 빈 값은 dedup하지 않는다.
    summary_findings = []
    seen_categories = set()
    for f in findings:
        _cat = str(f.get('cat2', '') or '').strip()
        _cat_key = _cat.casefold() if _cat and _cat.lower() != 'nan' else None
        if _cat_key is not None:
            if _cat_key in seen_categories:
                continue
            seen_categories.add(_cat_key)
        summary_findings.append(f)

    shown = summary_findings[:top_n]
    lis = []

    def _summary_title(f):
        """메일 Summary 제목은 유형·항목·wafer만 표시하고 수치 상세는 근거로 내린다."""
        _type_labels = {
            'SPEC_OUT': 'Spec-out', 'FLIER': 'Flier', 'DISPERSION': '산포 확대',
            'LEVEL_SHIFT': '수준 이동', 'TREND': '지속 Trend', 'SPC_RUN': 'SPC 연속 이상',
            'MEAS_SUSPECT': '측정이상 추정',
        }
        _ftype = str(f.get('type', '') or '').upper()
        if _ftype not in _type_labels:
            return str(f.get('title', '') or '')
        _name = str(f.get('display_name') or f.get('item') or '').strip()
        _title = f"{_type_labels[_ftype]}: {_name}" if _name else _type_labels[_ftype]
        _wids = list(f.get('affected_wafer_ids') or f.get('so_wafer_ids') or [])
        _clean_wids = []
        for _w in _wids:
            if _w in (None, '') or str(_w).lower() == 'nan':
                continue
            try:
                _wv = str(int(float(_w)))
            except (TypeError, ValueError):
                _wv = str(_w)
            if _wv not in _clean_wids:
                _clean_wids.append(_wv)
        if _clean_wids:
            _cap = 8
            _wf_text = ', '.join('#' + _w for _w in _clean_wids[:_cap])
            if len(_clean_wids) > _cap:
                _wf_text += f" 외 {len(_clean_wids) - _cap}매"
            _title += f" · wafer {_wf_text}"
        return _title

    for f in shown:
        _summary_text = ' '.join(str(f.get('basis') or f.get('detail') or '').split())
        if len(_summary_text) > 220:
            _summary_text = _summary_text[:217].rstrip() + '...'
        lis.append(
            f'<li style="margin-bottom:5px; list-style:none;">'
            f'{_sev_badge(f["severity"])} '
            f'<b>{_summary_title(f)}</b>'
            # 요약은 간결 근거(basis)만 — 어느 샷/wafer에서 spec-out인지 줄글 나열은 하지 않음.
            # basis 없으면(예: 산포 확대) 생략(제목에 이미 요지 포함). 상세 위치는 PPT 상세 페이지 참조.
            + (f'<br><span style="color:#555; font-size:12px;">근거: {_summary_text}</span>'
               if _summary_text else "")
            + '</li>')
    more = ""
    _deduped = len(findings) - len(summary_findings)
    if len(summary_findings) > top_n:
        more = (f'<div style="font-size:12px; color:#555; margin:4px 0 0;">'
                f'… 같은 카테고리는 최우선 1건만 선별하여 우선순위 상위 {top_n}건을 표시했습니다. '
                f'전체 {len(findings)}건의 상세는 '
                f'<b>{detail_ref}</b>를 참조하세요.</div>')
    elif _deduped > 0:
        more = (f'<div style="font-size:12px; color:#555; margin:4px 0 0;">'
                f'같은 카테고리 {_deduped}건은 우선순위가 가장 높은 대표 항목으로 요약했습니다.</div>')
    return head + ('<ul style="font-size:13px; color:#333; margin:5px 0 8px; padding-left:4px; list-style:none;">'
                   + "".join(lis) + '</ul>') + more + (tail_note or '')


