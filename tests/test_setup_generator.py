import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SetupGeneratorTest(unittest.TestCase):
    def test_setup_generates_yaml_only_skeleton_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shutil.copy2(ROOT / "Setup.py", tmp_path / "Setup.py")

            result = subprocess.run(
                [sys.executable, "Setup.py"],
                cwd=tmp_path,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

            expected = ["Main.py", "My_Function.py", "config.yaml", "README.md"]
            for name in expected:
                self.assertTrue((tmp_path / name).exists(), f"{name} was not generated")

            main_text = (tmp_path / "Main.py").read_text(encoding="utf-8")
            function_text = (tmp_path / "My_Function.py").read_text(encoding="utf-8")
            self.assertNotIn("My_config", main_text)
            self.assertNotIn("My_config", function_text)

            config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("VEHICLE_A", config)

            compile_result = subprocess.run(
                [sys.executable, "-B", "-m", "py_compile", "Main.py", "My_Function.py"],
                cwd=tmp_path,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr + compile_result.stdout)


if __name__ == "__main__":
    unittest.main()
