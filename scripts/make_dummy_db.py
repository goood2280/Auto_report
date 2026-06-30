# -*- coding: utf-8 -*-
"""
make_dummy_db.py — Auto Report 오프라인 더미 DB 생성기
=====================================================
vehicle_A(main) + Vehicle_B(with_vehicle) 비교 리포트를 오프라인에서 재현·검증하기 위한
풍부한 합성 픽스처를 생성한다. (bigdataquery mock 스키마를 그대로 사용)

생성물:
  - SF3_Data_Extractor_Input_File_v0.xlsx / Zone_Define  : vehicle_A + Vehicle_B 좌표(dense disc, inner-merge 통과용)
  - RUN/DB/vehicle_A_daily/date=*/data.parquet           : target lot(T1234.1) + 이력 lot 다수
  - RUN/DB/Vehicle_B_daily/date=*/data.parquet           : 값 시프트된 동반 lot(회색 cloud)
  - RUN/log/vehicle_A_et_log.csv / _et_log_Final.csv      : 기존 스키마 미러
  - RUN/DB/vehicle_A_wip_current.csv                      : 기존 스키마 미러

실행:  python scripts/make_dummy_db.py
검증:  python Main.py "_TRIGGER_vehicle_A_T1234.1_test"
"""
import os
import shutil
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

DB = os.path.join(BASE, 'RUN', 'DB')
LOG = os.path.join(BASE, 'RUN', 'log')
os.makedirs(DB, exist_ok=True)
os.makedirs(LOG, exist_ok=True)

NOW = datetime.now()

# ── 칩 좌표 (dense disc, 반경 2) ───────────────────────────────────────────
CHIPS = [(x, y) for x in range(-2, 3) for y in range(-2, 3) if x * x + y * y <= 4]  # 13 chips

# ── ET item 파라미터 (reformatter REAL ITEMID 기준) ────────────────────────
ITEM_PARAMS = {
    "ET_VTH_N":  {"mean": 0.50, "std": 0.05, "wv_shift": 0.10},
    "ET_VTH_P":  {"mean": 0.48, "std": 0.04, "wv_shift": 0.10},
    "ET_IDSAT_N": {"mean": 550.0, "std": 50.0, "wv_shift": 70.0},
    "ET_IDSAT_P": {"mean": 520.0, "std": 45.0, "wv_shift": 70.0},
}
ITEMS = list(ITEM_PARAMS.keys())


def _zone_define():
    """vehicle_A + Vehicle_B 두 mask에 대한 Zone_Define 시트 생성 (inner-merge 통과 필수)."""
    rows = []
    for mask in ['vehicle_A', 'Vehicle_B']:
        for (x, y) in CHIPS:
            rows.append({
                'MASK': mask, 'CHIP_X_POS': x, 'CHIP_Y_POS': y, 'FLAT_ZONE_POS': 0,
                'CHIP_X_ADJ': x, 'CHIP_Y_ADJ': y,
                'Chip_Radius': round(float(np.hypot(x, y)), 2),
            })
    df = pd.DataFrame(rows)
    out = os.path.join(BASE, 'SF3_Data_Extractor_Input_File_v0.xlsx')
    with pd.ExcelWriter(out, engine='openpyxl') as w:
        df.to_excel(w, sheet_name='Zone_Define', index=False)
    print(f"[OK] Zone_Define ({len(df)} rows, masks=vehicle_A/Vehicle_B) -> {out}")


def _et_rows(fab_lot_id, root_lot_id, the_date, wafers, chips, subitems, seed, wv_shift=False):
    """단일 lot의 ET row 리스트 생성 (mock 스키마와 동일)."""
    rng = np.random.default_rng(seed)
    base_time = the_date.replace(hour=3, minute=0, second=0, microsecond=0)
    rows = []
    for wf in wafers:
        # 웨이퍼당 측정 시각(분 오프셋)을 한 번 정해 같은 칩의 모든 item이 동일 tkout_time을 공유하도록 함
        # (pivot 인덱스에 tkout_time이 포함되므로, 같아야 REAL 값들이 한 행에 모여 ADDP 계산이 가능)
        wf_offset = int(rng.integers(0, 180))
        for (cx, cy) in chips:
            for sub in subitems:
                tk = (base_time + timedelta(minutes=wf_offset)).strftime("%Y-%m-%d %H:%M:%S")
                for item in ITEMS:
                    ip = ITEM_PARAMS[item]
                    val = (ip["mean"]
                           + (wf - 13) * ip["std"] * 0.02
                           + (cx + cy) * ip["std"] * 0.05
                           + rng.normal(0, ip["std"] * 0.4))
                    if wv_shift:
                        val += ip["wv_shift"]
                    rows.append({
                        "fab_lot_id": fab_lot_id, "lot_id": fab_lot_id, "root_lot_id": root_lot_id,
                        "wafer_id": str(wf), "process_id": "proc", "part_id": "PART_A",
                        "step_id": "test", "step_seq": "N02V98HI",
                        "tkout_time": tk,
                        "item_id": item, "flat_zone": "0",
                        "eqp_id": f"EQP_{(wf % 3) + 1:02d}", "probe_card_id": "PC_01",
                        "chip_x_pos": str(cx), "chip_y_pos": str(cy), "subitem_id": sub,
                        "et_value": str(round(float(val), 6)), "temperature": "25",
                        "total_site_cnt": str(len(chips)),
                    })
    return rows


def _write_daily(vehicle, lots):
    """lots: list of (fab_lot_id, root_lot_id, days_ago, wafers, chips, subitems, wv_shift). date= 파티션으로 저장."""
    daily_dir = os.path.join(DB, f'{vehicle}_daily')
    if os.path.isdir(daily_dir):
        shutil.rmtree(daily_dir)
    os.makedirs(daily_dir, exist_ok=True)
    per_date = {}
    for i, (flot, rlot, days_ago, wafers, chips, subs, wv) in enumerate(lots):
        d = NOW - timedelta(days=days_ago)
        rows = _et_rows(flot, rlot, d, wafers, chips, subs, seed=1000 + i, wv_shift=wv)
        per_date.setdefault(d.strftime("%Y-%m-%d"), []).extend(rows)
    total = 0
    for dstr, rows in per_date.items():
        pdir = os.path.join(daily_dir, f'date={dstr}')
        os.makedirs(pdir, exist_ok=True)
        pd.DataFrame(rows).to_parquet(os.path.join(pdir, 'data.parquet'), index=False)
        total += len(rows)
    print(f"[OK] {vehicle}_daily: {len(per_date)} dates, {total} rows -> {daily_dir}")


def _write_logs(va_lots):
    """et_log / et_log_Final / wip_current 를 기존 스키마로 재생성 (vehicle_A 기준)."""
    wafers_repr = "[" + ", ".join(str(w) for w in range(1, 26)) + "]"
    et_rows, final_rows, wip_rows = [], [], []
    for (flot, rlot, days_ago, *_rest) in va_lots:
        d = NOW - timedelta(days=days_ago)
        ts = d.replace(hour=3, minute=16, second=58).strftime("%Y-%m-%d %H:%M:%S")
        pk = f"vehicle_A_{flot}_test"
        et_rows.append({"prime_key": pk, "wafer_id": wafers_repr, "step_seq": "['N02V98HI']",
                        "total_site_cnt": f"[{len(CHIPS)}]", "tkout_time": ts})
        final_rows.append({"prime_key": pk, "wafer_id": wafers_repr, "step_seq": "['N02V98HI']",
                           "total_site_cnt": f"[{len(CHIPS)}]", "tkout_time": ts,
                           "lot_id": flot, "dc_step_id": "test", "dc_done": True})
        wip_rows.append({"line_id": "line", "lot_id": flot, "step_id": "FI100", "lot_current_loc": "STOCK",
                         "last_update_date": (d + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                         "lot_id6": rlot})
    pd.DataFrame(et_rows).to_csv(os.path.join(LOG, 'vehicle_A_et_log.csv'), index=False)
    pd.DataFrame(final_rows).to_csv(os.path.join(LOG, 'vehicle_A_et_log_Final.csv'), index=False)
    pd.DataFrame(wip_rows).to_csv(os.path.join(DB, 'vehicle_A_wip_current.csv'), index=False, encoding='cp949')
    print(f"[OK] et_log / et_log_Final / wip_current 재생성 ({len(va_lots)} lots)")


def main():
    full = CHIPS
    center = [(x, y) for (x, y) in CHIPS if abs(x) <= 1 and abs(y) <= 1]  # 5 chips
    w25 = list(range(1, 26))
    w12 = list(range(1, 13))

    # vehicle_A: target(T1234.1, 오늘, full) + 이력 lot 다수 (cloud band 형성)
    va_lots = [
        ("T1234.1", "T1234", 0,  w25, full,   ["MAIN", "EDGE"], False),  # target (red)
        ("T5678.1", "T5678", 3,  w12, center, ["MAIN"],          False),
        ("T9012.1", "T9012", 7,  w12, center, ["MAIN"],          False),
        ("T3456.1", "T3456", 12, w12, center, ["MAIN"],          False),
        ("T2233.1", "T2233", 18, w12, center, ["MAIN"],          False),
        ("T4455.1", "T4455", 24, w12, center, ["MAIN"],          False),
        ("T6677.1", "T6677", 30, w12, center, ["MAIN"],          False),
    ]
    # Vehicle_B (with_vehicle): 값 시프트 → 회색 cloud 분리
    vb_lots = [
        ("VB001.1", "VB001", 2,  w12, center, ["MAIN"], True),
        ("VB002.1", "VB002", 9,  w12, center, ["MAIN"], True),
        ("VB003.1", "VB003", 16, w12, center, ["MAIN"], True),
        ("VB004.1", "VB004", 26, w12, center, ["MAIN"], True),
    ]

    _zone_define()
    _write_daily('vehicle_A', va_lots)
    _write_daily('Vehicle_B', vb_lots)
    _write_logs(va_lots)
    print("\n[DONE] 더미 DB 생성 완료. 다음으로 실행:")
    print('       python Main.py "_TRIGGER_vehicle_A_T1234.1_test"')


if __name__ == "__main__":
    main()
