from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def write_reformatter() -> None:
    path = ROOT / "reformatter" / "VEHICLE_A_reformatter.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {
                "ALIAS": "ADDP_ITEM_01",
                "REPORT ORDER": 1,
                "ADDP Form": "({REAL_ITEM_A} + {REAL_ITEM_B}) / 2",
                "SPECLOW": 0,
                "SPECHIGH": 100,
                "col_unit": "arb.",
                "col_direction": "BOTH",
                "REPORT LOG SCALE": False,
                "CAT2": "CAT_A",
            }
        ]
    )
    df.to_csv(path, index=False)
    print(f"created {path.relative_to(ROOT)}")


def write_coordinate_workbook() -> None:
    path = ROOT / "fixtures" / "coordinate_template.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for x in range(-1, 2):
        for y in range(-1, 2):
            rows.append(
                {
                    "MASK": "MASK_A",
                    "CHIP_X_POS": x,
                    "CHIP_Y_POS": y,
                    "FLAT_ZONE_POS": "N",
                    "PGM(pt)": "PGM_01",
                    "CHIP_X_ADJ": x,
                    "CHIP_Y_ADJ": y,
                    "Chip_Radius": (x**2 + y**2) ** 0.5,
                }
            )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Zone_Define", index=False)
        pd.DataFrame([{"note": "Temporary anonymized coordinate fixture"}]).to_excel(
            writer, sheet_name="README", index=False
        )
    print(f"created {path.relative_to(ROOT)}")


def write_columnbase_file() -> None:
    path = ROOT / "BASE_DIR" / "DB" / "columnbase" / "VEHICLE_A_columnbase.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for wafer_id in range(1, 4):
        for x in range(-1, 2):
            for y in range(-1, 2):
                for item, value in {
                    "REAL_ITEM_A": 40 + wafer_id * 10 + x,
                    "REAL_ITEM_B": 44 + wafer_id * 10 - y,
                }.items():
                    rows.append(
                        {
                            "fab_lot_id": "LOT001",
                            "lot_id": "LOT001",
                            "root_lot_id": "LOT00",
                            "wafer_id": wafer_id,
                            "process_id": "PROCESS_ID_A",
                            "part_id": "PART_A",
                            "step_id": "STEP001",
                            "step_seq": 1,
                            "tkout_time": "2026-01-01 00:00:00",
                            "flat_zone": "N",
                            "eqp_id": "EQP_01",
                            "probe_card_id": "PC_01",
                            "chip_x_pos": x,
                            "chip_y_pos": y,
                            "subitem_id": "SUB_01",
                            "temperature": 25,
                            "total_site_cnt": 9,
                            "item_id": item,
                            "et_value": value,
                        }
                    )
    pd.DataFrame(rows).to_parquet(path, index=False)
    print(f"created {path.relative_to(ROOT)}")


def main() -> None:
    write_reformatter()
    write_coordinate_workbook()
    write_columnbase_file()
    (ROOT / "BASE_DIR" / "Report" / "VEHICLE_A" / "Mail").mkdir(parents=True, exist_ok=True)
    (ROOT / "BASE_DIR" / "Report" / "VEHICLE_A" / "HTML").mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
