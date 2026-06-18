import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert "VEHICLE_A" in config
    assert (ROOT / "reformatter" / "VEHICLE_A_reformatter.csv").exists()
    assert (ROOT / "BASE_DIR" / "DB" / "columnbase" / "VEHICLE_A_columnbase.parquet").exists()
    pd.read_csv(ROOT / "reformatter" / "VEHICLE_A_reformatter.csv")

    subprocess.run(
        [sys.executable, "-B", "Main.py", "VEHICLE_A"],
        cwd=ROOT,
        check=True,
    )
    print("smoke check ok")


if __name__ == "__main__":
    main()
