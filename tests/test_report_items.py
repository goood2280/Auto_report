"""Offline contracts for persisted report item selection."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_items
import My_Function as mf


def formatter():
    return pd.DataFrame([
        {"ALIAS": "A", "REPORT ORDER": 1, "CAT2": "Electrical", "CATEGORY": "REAL",
         "tkout_time": "SHOULD_NOT_BE_READ", "split_check": "SHOULD_NOT_BE_READ"},
        {"ALIAS": "B", "REPORT ORDER": 2, "CAT2": "  ", "CATEGORY": "REAL"},
        {"ALIAS": "C", "REPORT ORDER": None, "CAT2": "Electrical", "CATEGORY": "REAL"},
        {"ALIAS": "D", "REPORT ORDER": "bad", "CAT2": "Electrical", "CATEGORY": "REAL"},
        {"ALIAS": "E", "REPORT ORDER": float("inf"), "CAT2": "Electrical", "CATEGORY": "REAL"},
    ])


def test_legacy_default_and_explicit_empty_and_blank_category():
    selected, options = report_items.select_formatter(formatter(), "V", "daily_trend", {})
    assert selected.ALIAS.tolist() == ["A", "B"]
    assert selected.CAT2.tolist() == ["Electrical", "Uncategorized"]
    assert options == {"A": {"time_column": "", "split_columns": []},
                       "B": {"time_column": "", "split_columns": []}}
    selected, options = report_items.select_formatter(formatter(), "V", "daily_trend",
                                                       {"daily_trend": {"V": {}}})
    assert selected.empty and options == {}


def test_update_first_edit_materializes_default_then_add_remove_and_preserves_other_service(tmp_path):
    path = tmp_path / "reformatter" / "report_items.yaml"
    f = formatter()
    report_items.update_items(path, f, "V", "daily_trend", remove=["A"])
    catalog = report_items.load_catalog(path)
    assert set(catalog["daily_trend"]["V"]) == {"B"}
    assert "V" not in catalog["mlmode"]
    report_items.update_items(path, f, "V", ["daily_trend", "mlmode"], add=["A"],
                              options={"A": {"time_column": "ETCH_TIME", "split_columns": ["KNOB_ETCH"]}})
    catalog = report_items.load_catalog(path)
    assert set(catalog["daily_trend"]["V"]) == {"A", "B"}
    assert catalog["daily_trend"]["V"]["A"]["time_column"] == "ETCH_TIME"
    assert set(catalog["mlmode"]["V"]) == {"A", "B"}
    assert report_items.select_formatter(f, "V", "mlmode", catalog)[0].ALIAS.tolist() == ["A", "B"]


@pytest.mark.parametrize("bad", [
    {"daily_trend": {}},
    {"version": 2},
    {"version": 1, "other": {}},
    {"version": 1, "daily_trend": []},
    {"version": 1, "daily_trend": {"V": {"A": {"split_columns": "x"}}}},
    {"version": 1, "daily_trend": {"V": {"A": {"tkout_time": "x"}}}},
])
def test_strict_schema_rejects_bad_catalog(tmp_path, bad):
    path = tmp_path / "report_items.yaml"
    import yaml
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        report_items.load_catalog(path)


def test_unknown_and_non_report_aliases_fail(tmp_path):
    with pytest.raises(ValueError, match="unknown or ineligible"):
        report_items.select_formatter(formatter(), "V", "daily_trend",
                                      {"daily_trend": {"V": {"C": {}}}})
    with pytest.raises(ValueError, match="unknown or ineligible"):
        report_items.update_items(tmp_path / "catalog.yaml", formatter(), "V", "mlmode", add=["C"])


def test_daily_trend_ml_uses_catalog_columns_and_ignores_formatter_options(tmp_path):
    ml_table = pd.DataFrame({
        "root_lot_id": ["L1", "L2"], "wafer_id": [1, 2],
        "TKOUT_TIME_PROC": ["20260930090000", "20260930091000"],
        "KNOB_ETCH": ["E1", "E2"], "FAB_RECIPE_A": ["R1", "R2"],
        "FAB_RECIPE_B": ["R3", "R4"], "BAD_TIME": ["19990101000000"] * 2,
        "BAD_SPLIT": ["bad"] * 2,
    })
    ml_table.to_parquet(tmp_path / "ML_TABLE_V.parquet", index=False)
    # These tempting legacy fields must have no effect on column selection.
    reformatter = pd.DataFrame([{"ALIAS": "A", "tkout_time": "BAD_TIME", "split_check": "BAD_SPLIT"}])
    settings = {
        "service": "daily_trend", "ml_table_dir": str(tmp_path),
        "_item_options": {"A": {"time_column": "PROC", "split_columns": ["ETCH", "FAB_RECIPE_*", "MISSING"]}},
    }
    frame = pd.DataFrame({"root_lot_id": ["L1", "L2"], "wafer_id": [1, 2], "A": [1., 2.]})
    merged, mappings = mf.daily_trend_ml(frame, reformatter, "V", settings)
    mapping = mappings["A"]
    assert mapping["time_columns"] == ["TKOUT_TIME_PROC"]
    assert mapping["split_columns"] == ["KNOB_ETCH", "FAB_RECIPE_A", "FAB_RECIPE_B"]
    assert any("MISSING" in warning for warning in mapping["warnings"])
    assert "__ml_BAD_TIME" not in merged and "__ml_BAD_SPLIT" not in merged
    assert merged["__ml_TKOUT_TIME_PROC"].tolist() == ["20260930090000", "20260930091000"]
    assert merged["__ml_KNOB_ETCH"].tolist() == ["E1", "E2"]


def test_duplicate_alias_is_selected_only_from_finite_report_order_row():
    f = pd.DataFrame([
        {"ALIAS": "A", "REPORT ORDER": None, "CAT2": "invalid duplicate"},
        {"ALIAS": "A", "REPORT ORDER": 1, "CAT2": "valid duplicate"},
        {"ALIAS": "B", "REPORT ORDER": None, "CAT2": "ineligible"},
    ])
    selected, _ = report_items.select_formatter(f, "V", "daily_trend", {})
    assert len(selected) == 1
    assert selected.iloc[0]["CAT2"] == "valid duplicate"


def test_service_and_product_selection_are_independent():
    catalog = {"version": 1,
               "daily_trend": {"V": {"A": {}}},
               "mlmode": {"V": {"B": {}}, "OTHER": {}}}
    f = formatter()
    assert report_items.select_formatter(f, "V", "daily_trend", catalog)[0].ALIAS.tolist() == ["A"]
    assert report_items.select_formatter(f, "V", "mlmode", catalog)[0].ALIAS.tolist() == ["B"]
    assert report_items.select_formatter(f, "OTHER", "mlmode", catalog)[0].empty
    assert report_items.select_formatter(f, "OTHER", "daily_trend", catalog)[0].ALIAS.tolist() == ["A", "B"]
