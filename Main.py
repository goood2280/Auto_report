import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from My_Function import (
    build_report_artifacts,
    prepare_report_dataframe,
    reformatter_verify,
    required_real_items,
)


def deep_merge(base, override):
    result = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def expand_value(value, context):
    if isinstance(value, str):
        return value.format(**context)
    if isinstance(value, list):
        return [expand_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: expand_value(item, context) for key, item in value.items()}
    return value


def load_config(product_name, config_path="config.yaml"):
    if load_dotenv:
        load_dotenv()

    with open(config_path, "r", encoding="utf-8") as file:
        config_data = yaml.safe_load(file) or {}

    if product_name not in config_data:
        raise KeyError(f"{product_name} is not defined in {config_path}")

    config = deep_merge(config_data.get("defaults", {}), config_data[product_name])
    config["product_name"] = product_name
    config.setdefault("vehicle", product_name)

    context = {
        "product": product_name,
        "vehicle": config.get("vehicle", product_name),
        "root": config.get("root", "BASE_DIR"),
    }
    config = expand_value(config, context)

    for key, value in os.environ.items():
        config.setdefault(key, value)

    return config


def path_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def read_table(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    raise ValueError(f"Unsupported data file type: {path}")


def load_columnbase_data(config):
    files = []
    for pattern in path_list(config.get("columnbase_files")):
        matches = glob.glob(pattern)
        files.extend(matches if matches else [pattern])

    if not files:
        raise FileNotFoundError("No columnbase_files configured in config.yaml")

    missing = [file for file in files if not Path(file).exists()]
    if missing:
        raise FileNotFoundError(f"Columnbase files not found: {missing}")

    frames = [read_table(file) for file in files]
    return pd.concat(frames, ignore_index=True)


def ensure_output_dirs(config):
    for key in ["root", "report_path", "mail_ppt_path", "html_path"]:
        Path(config[key]).mkdir(parents=True, exist_ok=True)


def choose_target_groups(report_df, config):
    target_lot = config.get("target_lot")
    target_step = config.get("target_step_id")
    if target_lot and target_step:
        yield str(target_lot), str(target_step), report_df[
            (report_df["fab_lot_id"].astype(str) == str(target_lot))
            & (report_df["step_id"].astype(str) == str(target_step))
        ]
        return

    group_cols = [col for col in ["fab_lot_id", "step_id"] if col in report_df.columns]
    if len(group_cols) < 2:
        yield str(config.get("fallback_lot", "LOT001")), str(config.get("fallback_step", "STEP001")), report_df
        return

    for (lot_id, step_id), group in report_df.groupby(group_cols, dropna=False):
        yield str(lot_id), str(step_id), group


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate Auto Report mail HTML and PPT from columnbase data.")
    parser.add_argument("product", help="Product key in config.yaml, for example VEHICLE_A")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args(argv)

    config = load_config(args.product, args.config)
    ensure_output_dirs(config)

    df_reformatter = pd.read_csv(config["reformatter_path"])
    if not reformatter_verify(df_reformatter):
        return 2

    needed_items = sorted(required_real_items(df_reformatter))
    if needed_items:
        print(f"Required real items: {', '.join(needed_items)}")

    columnbase_df = load_columnbase_data(config)
    report_df = prepare_report_dataframe(columnbase_df, df_reformatter, config)
    if report_df.empty:
        raise ValueError("No report data was created from columnbase input")

    upload_date = datetime.now().strftime("%Y%m%d")
    generated = []
    for target_lot, target_step_id, df_target in choose_target_groups(report_df, config):
        if df_target.empty:
            continue
        target_root = str(df_target.get("root_lot_id", pd.Series([target_lot[:5]])).iloc[0])
        artifacts = build_report_artifacts(
            cfg=config,
            df_target=df_target,
            df_reformatter=df_reformatter,
            target_lot=target_lot,
            target_root=target_root,
            target_step_id=target_step_id,
            upload_date=upload_date,
        )
        generated.append(artifacts)
        print(f"HTML: {artifacts['html_path']} ({artifacts['html_size']} bytes)")
        print(f"PPTX: {artifacts['ppt_path']} ({artifacts['ppt_size']} bytes)")

    if not generated:
        raise ValueError("No target groups generated report artifacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
