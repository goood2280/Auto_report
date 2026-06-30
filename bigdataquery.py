"""
bigdataquery - 사내 빅데이터 플랫폼 Mock 모듈
===============================================
사내 이식 시 이 파일을 실제 bigdataquery 패키지로 교체하세요.
이 Mock은 로컬 테스트를 위해 합성 반도체 측정 데이터를 생성합니다.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def getData(params, custom_columns=None, user_name=None):
    """
    사내 빅데이터 플랫폼 쿼리 Mock.

    Args:
        params: dict - 쿼리 파라미터
            table_name, dateFrom, dateTo, process_id, line_id,
            item_id, root_lot_id, step_seq, not_like_conditions, like_conditions
        custom_columns: list - 반환할 컬럼 목록
        user_name: str - 사용자 ID

    Returns:
        pd.DataFrame
    """
    table_name = params.get("table_name", "")
    print(f"[MOCK-bigdataquery] getData: table={table_name}, user={user_name}")

    if "f_et_test" in table_name:
        return _mock_et_test(params, custom_columns)
    elif "f_fab_wf_met" in table_name:
        return _mock_inline_data(params, custom_columns)
    elif "f_wip_current" in table_name:
        return _mock_wip_data(params, custom_columns)
    else:
        print(f"[MOCK-bigdataquery] Unknown table: {table_name}")
        return pd.DataFrame(columns=custom_columns or [])


# ---------------------------------------------------------------------------
# ET 전기적 테스트 데이터
# ---------------------------------------------------------------------------
def _mock_et_test(params, custom_columns):
    np.random.seed(42)

    process_id_raw = params.get("process_id", ["proc"])
    process_id = process_id_raw[0] if isinstance(process_id_raw, list) else process_id_raw
    line_id_raw = params.get("line_id", ["line"])
    line_id = line_id_raw[0] if isinstance(line_id_raw, list) else line_id_raw
    item_ids = params.get("item_id", ["ET_VTH_N", "ET_VTH_P", "ET_IDSAT_N", "ET_IDSAT_P"])

    lots = [
        {"fab_lot_id": "T1234.1", "lot_id": "T1234.1", "root_lot_id": "T1234"},
        {"fab_lot_id": "T5678.1", "lot_id": "T5678.1", "root_lot_id": "T5678"},
    ]
    wafers = [1, 2, 3]
    chips = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 0), (0, 1), (1, -1), (1, 0), (1, 1)]

    item_params = {
        "ET_VTH_N":  {"mean": 0.50, "std": 0.05},
        "ET_VTH_P":  {"mean": 0.48, "std": 0.04},
        "ET_IDSAT_N": {"mean": 550,  "std": 50},
        "ET_IDSAT_P": {"mean": 520,  "std": 45},
    }

    base_time = datetime.now() - timedelta(hours=3)
    rows = []
    for lot in lots:
        for wf in wafers:
            for cx, cy in chips:
                for item in item_ids:
                    ip = item_params.get(item, {"mean": 50, "std": 5})
                    wf_off = (wf - 2) * ip["std"] * 0.3
                    chip_off = (cx + cy) * ip["std"] * 0.1
                    value = ip["mean"] + wf_off + chip_off + np.random.normal(0, ip["std"] * 0.5)
                    rows.append({
                        "fab_lot_id": lot["fab_lot_id"],
                        "lot_id": lot["lot_id"],
                        "root_lot_id": lot["root_lot_id"],
                        "wafer_id": str(wf),
                        "process_id": process_id,
                        "part_id": "PART_A",
                        "step_id": "test",
                        "step_seq": "N02V98HI",
                        "tkout_time": (base_time + timedelta(minutes=np.random.randint(0, 30))).strftime("%Y-%m-%d %H:%M:%S"),
                        "item_id": item,
                        "flat_zone": "0",
                        "eqp_id": f"EQP_{(wf % 3) + 1:02d}",
                        "probe_card_id": "PC_01",
                        "chip_x_pos": str(cx),
                        "chip_y_pos": str(cy),
                        "subitem_id": "MAIN",
                        "et_value": str(round(value, 6)),
                        "temperature": "25",
                        "total_site_cnt": "9",
                    })

    df = pd.DataFrame(rows)
    if custom_columns:
        available = [c for c in custom_columns if c in df.columns]
        df = df[available]
    print(f"[MOCK-bigdataquery] ET rows={len(df)}")
    return df


# ---------------------------------------------------------------------------
# Inline FAB 계측 데이터
# ---------------------------------------------------------------------------
def _mock_inline_data(params, custom_columns):
    np.random.seed(43)

    root_lot_id = params.get("root_lot_id", "T1234")
    step_seq_list = params.get("step_seq", ["S100", "S200", "S300"])

    inline_items = {
        "S100": [{"item_id": "THK01", "mean": 50.0, "std": 2.0,
                  "sh": 55, "sl": 50, "slow": 45}],
        "S200": [{"item_id": "CD01", "mean": 0.022, "std": 0.001,
                  "sh": 0.025, "sl": 0.022, "slow": 0.019}],
        "S300": [{"item_id": "DOSE01", "mean": 1e15, "std": 5e13,
                  "sh": 1.1e15, "sl": 1e15, "slow": 0.9e15}],
    }

    base_time = datetime.now() - timedelta(hours=5)
    rows = []
    for ssq in step_seq_list:
        items = inline_items.get(ssq, [])
        for ii in items:
            for wf in [1, 2, 3]:
                val = ii["mean"] + (wf - 2) * ii["std"] * 0.2 + np.random.normal(0, ii["std"] * 0.3)
                rows.append({
                    "root_lot_id": root_lot_id,
                    "wafer_id": str(wf),
                    "tkout_time": (base_time + timedelta(minutes=np.random.randint(0, 60))).strftime("%Y-%m-%d %H:%M:%S"),
                    "step_id": ssq,
                    "item_id": ii["item_id"],
                    "fab_value": str(round(val, 6)),
                    "spc_ctrl_spec_high": str(ii["sh"]),
                    "spc_ctrl_spec_limit": str(ii["sl"]),
                    "spc_ctrl_spec_low": str(ii["slow"]),
                    "step_seq": ssq,
                })

    df = pd.DataFrame(rows)
    if custom_columns:
        keep = [c for c in custom_columns if c in df.columns]
        if "step_seq" not in keep and "step_seq" in df.columns:
            keep.append("step_seq")
        df = df[keep]
    print(f"[MOCK-bigdataquery] Inline rows={len(df)}")
    return df


# ---------------------------------------------------------------------------
# WIP 현재 상태
# ---------------------------------------------------------------------------
def _mock_wip_data(params, custom_columns):
    rows = [
        {"line_id": "line", "lot_id": "T1234.1", "step_seq": "FI100",
         "lot_current_loc": "STOCK",
         "last_update_date": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")},
        {"line_id": "line", "lot_id": "T5678.1", "step_seq": "FI100",
         "lot_current_loc": "STOCK",
         "last_update_date": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")},
    ]
    df = pd.DataFrame(rows)
    if custom_columns:
        available = [c for c in custom_columns if c in df.columns]
        df = df[available]
    print(f"[MOCK-bigdataquery] WIP rows={len(df)}")
    return df
