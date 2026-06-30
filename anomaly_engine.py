# -*- coding: utf-8 -*-
"""
anomaly_engine - 이상탐지 파이프라인 Mock 모듈
================================================

이 모듈은 ET 측정 데이터에서 이상 항목(anomaly)을 감지하고,
감지된 항목에 대한 Trend 차트를 생성하여 HTML 이미지로 반환합니다.

주요 기능:
    - Z-Score 기반 이상 항목 탐지
    - 이상 항목 Trend 차트 생성 (3열 × 2행 그리드)
    - 설정(config) 객체를 통한 임계값·차트 파라미터 주입

사내 이식 시 실제 이상탐지 엔진으로 교체하세요.

사용법:
    from anomaly_engine import run_anomaly_pipeline

    result = run_anomaly_pipeline(
        merged_df, root_lot_id, dc_step,
        spec_data=spec_df, reformatter=ref_df,
        config=my_config  # 선택 — 없으면 기본값 사용
    )
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
