"""Offline tests for immutable local runtime code snapshots and rollback."""
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime_versions as versions


def populate(root, marker="old", *, loose=(), zip_present=True, include_report_items=True,
             infrastructure=b"# infrastructure member"):
    root.mkdir(parents=True, exist_ok=True)
    code = {name: f"# {marker} {name}\nVALUE = {marker!r}\n".encode()
            for name in versions.RUNTIME_CODE}
    if not include_report_items:
        code.pop("report_items.py")
    for name in loose:
        (root / name).write_bytes(code[name])
    if zip_present:
        with zipfile.ZipFile(root / "auto_report_runtime.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name in code:
                if name not in loose:
                    archive.writestr(name, code[name])
            archive.writestr("runtime_versions.py", infrastructure)
            archive.writestr("other-helper.py", b"# unrelated member")
    return code


def read_code(root, name):
    path = root / name
    if path.exists():
        return path.read_bytes()
    with zipfile.ZipFile(root / "auto_report_runtime.zip") as archive:
        return archive.read(name)


def test_snapshot_uses_loose_precedence_and_duplicate_snapshot_is_immutable(tmp_path, monkeypatch):
    root = tmp_path / "install"
    code = populate(root, "zip", loose=("Main.py", "Scheduler.py"))
    loose_override = b"# local loose override\nVALUE = 'loose'\n"
    (root / "My_Function.py").write_bytes(loose_override)
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    first = versions.snapshot(root, "release A")
    same = versions.snapshot(root, "ignored duplicate label")
    assert first["id"] == same["id"]
    assert same["label"] == "release A"
    assert (tmp_path / "history" / first["id"] / "My_Function.py").read_bytes() == loose_override
    assert first["hashes"]["Main.py"] == versions._hashes(code)["Main.py"]
    assert len(versions.list_versions(root)) == 1
    assert versions.current_version(root)["id"] == first["id"]
    events = (tmp_path / "history" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1


def test_content_change_creates_new_version_and_store_override_is_used(tmp_path, monkeypatch):
    root = tmp_path / "install"
    populate(root, "one", loose=("Main.py", "Scheduler.py"))
    store = tmp_path / "history"
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(store))
    first = versions.snapshot(root, "one")
    populate(root, "two", loose=versions.RUNTIME_CODE)
    second = versions.snapshot(root, "two")
    assert first["id"] != second["id"]
    assert {item["label"] for item in versions.list_versions(root)} == {"one", "two"}
    assert versions.current_version(root)["status"] == "current"
    assert not (root / ".runtime-versions").exists()


def test_rollback_restores_runtime_only_and_preserves_zip_extras_and_source_shape(tmp_path, monkeypatch):
    root = tmp_path / "install"
    first_code = populate(root, "old", loose=("Main.py", "Scheduler.py", "My_Function.py"))
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    for name, content in {
        "My_config.py": b"# operator settings stay current\n",
        ".env": b"SECRET=do-not-touch\n",
        "reformatter/config.yaml": b"vehicle: current\n",
        "reformatter/report_items.yaml": b"version: 1\n",
        "reformatter/TEST_reformatter.csv": b"ALIAS,REPORT ORDER\nA,1\n",
        "RUN/DB/keep.parquet": b"synthetic data bytes",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    preserved = {name: (root / name).read_bytes() for name in
                 ("My_config.py", ".env", "reformatter/config.yaml", "reformatter/report_items.yaml",
                  "reformatter/TEST_reformatter.csv", "RUN/DB/keep.parquet")}
    old = versions.snapshot(root, "old")
    populate(root, "new", loose=("Main.py", "Scheduler.py", "anomaly_engine.py"))
    versions.snapshot(root, "new")
    assert (root / "anomaly_engine.py").exists()
    result = versions.rollback(root, old["id"])
    assert result["id"] == old["id"]
    for name, content in first_code.items():
        assert read_code(root, name) == content
    assert not (root / "anomaly_engine.py").exists()  # loose only in the newer source layout
    assert (root / "My_Function.py").exists()
    for name, content in preserved.items():
        assert (root / name).read_bytes() == content
    with zipfile.ZipFile(root / "auto_report_runtime.zip") as archive:
        assert archive.read("runtime_versions.py") == b"# infrastructure member"
        assert archive.read("other-helper.py") == b"# unrelated member"
    assert versions.current_version(root)["id"] == old["id"]
    assert [event["event"] for event in map(__import__("json").loads,
                (tmp_path / "history" / "events.jsonl").read_text(encoding="utf-8").splitlines())] == [
                    "snapshot", "snapshot", "rollback"]


def test_tampered_version_is_rejected_before_any_runtime_write(tmp_path, monkeypatch):
    root = tmp_path / "install"
    populate(root, "before", loose=("Main.py", "Scheduler.py"))
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    saved = versions.snapshot(root, "trusted")
    before = {name: read_code(root, name) for name in versions.RUNTIME_CODE}
    (tmp_path / "history" / saved["id"] / "Main.py").write_bytes(b"# tampered")
    with pytest.raises(versions.RuntimeVersionError, match="hash mismatch"):
        versions.rollback(root, saved["id"])
    assert {name: read_code(root, name) for name in versions.RUNTIME_CODE} == before


def test_rollback_refuses_while_another_process_holds_reader_lease(tmp_path, monkeypatch):
    root = tmp_path / "install"
    populate(root, "lease", loose=("Main.py", "Scheduler.py"))
    store = tmp_path / "history"
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(store))
    saved = versions.snapshot(root, "lease")
    ready = tmp_path / "lease-ready"
    env = dict(os.environ, AUTO_REPORT_VERSION_STORE=str(store), PYTHONIOENCODING="utf-8")
    script = """import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import runtime_versions as v
with v.runtime_lease(sys.argv[2]):
    pathlib.Path(sys.argv[3]).write_text('ready')
    time.sleep(20)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(ROOT), str(root), str(ready)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), child.stderr.read().decode("utf-8", errors="replace")
        with pytest.raises(versions.RuntimeLeaseError, match="in use"):
            versions.rollback(root, saved["id"])
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=10)


def test_partial_apply_failure_restores_previous_code_and_zip(tmp_path, monkeypatch):
    root = tmp_path / "install"
    populate(root, "prior", loose=("Main.py", "Scheduler.py", "My_Function.py"))
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    target = versions.snapshot(root, "target")
    populate(root, "current", loose=("Main.py", "Scheduler.py", "anomaly_engine.py"))
    versions.snapshot(root, "current")
    before_code = {name: read_code(root, name) for name in versions.RUNTIME_CODE}
    before_zip = (root / "auto_report_runtime.zip").read_bytes()
    real_atomic = versions._atomic_bytes
    did_fail = False

    def fail_once(path, data):
        nonlocal did_fail
        if Path(path) == root / "Scheduler.py" and not did_fail:
            did_fail = True
            raise OSError("injected replacement failure")
        return real_atomic(path, data)

    monkeypatch.setattr(versions, "_atomic_bytes", fail_once)
    with pytest.raises(versions.RuntimeVersionError, match="rollback failed"):
        versions.rollback(root, target["id"])
    assert did_fail
    assert {name: read_code(root, name) for name in versions.RUNTIME_CODE} == before_code
    assert (root / "auto_report_runtime.zip").read_bytes() == before_zip


def test_legacy_install_without_optional_selector_can_be_snapshotted_and_rolled_back(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    legacy_code = populate(root, "legacy", loose=("Main.py", "Scheduler.py"),
                           include_report_items=False)
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    saved = versions.snapshot(root, "pre-selector install")
    assert "report_items.py" not in saved["files"]
    assert set(saved["files"]) == set(versions.REQUIRED_RUNTIME_CODE)

    # Simulate a newer runtime adding the selector as a loose source and ZIP member.
    selector = root / "report_items.py"
    selector.write_bytes(b"# newly added selector\nVALUE = 'new'\n")
    with zipfile.ZipFile(root / "auto_report_runtime.zip", "a") as archive:
        archive.writestr("report_items.py", selector.read_bytes())
    versions.rollback(root, saved["id"])

    assert not selector.exists()
    with zipfile.ZipFile(root / "auto_report_runtime.zip") as archive:
        assert "report_items.py" not in archive.namelist()
        assert archive.read("runtime_versions.py") == b"# infrastructure member"
        assert archive.read("other-helper.py") == b"# unrelated member"
    for name, content in legacy_code.items():
        assert read_code(root, name) == content


def test_rollback_to_loose_source_keeps_current_zip_infrastructure(tmp_path, monkeypatch):
    root = tmp_path / "source-checkout"
    source_code = populate(root, "source", loose=versions.REQUIRED_RUNTIME_CODE,
                           zip_present=False, include_report_items=False)
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    saved = versions.snapshot(root, "source tree")
    assert not saved["zip_present"]

    populate(root, "compact-current", loose=("Main.py", "Scheduler.py"),
             infrastructure=b"# current infrastructure")
    with zipfile.ZipFile(root / "auto_report_runtime.zip", "a") as archive:
        archive.writestr("unrelated.py", b"# current unrelated member")
    versions.rollback(root, saved["id"])

    assert (root / "auto_report_runtime.zip").is_file()
    for name, content in source_code.items():
        assert read_code(root, name) == content
    with zipfile.ZipFile(root / "auto_report_runtime.zip") as archive:
        assert "report_items.py" not in archive.namelist()
        assert archive.read("runtime_versions.py") == b"# current infrastructure"
        assert archive.read("unrelated.py") == b"# current unrelated member"


def test_same_timestamp_and_size_rollback_uses_restored_source_not_cached_code(tmp_path, monkeypatch):
    import importlib.util
    import py_compile

    root = tmp_path / "install"
    monkeypatch.setenv("AUTO_REPORT_VERSION_STORE", str(tmp_path / "history"))
    populate(root, "old", loose=versions.RUNTIME_CODE)
    saved = versions.snapshot(root, "old")
    populate(root, "new", loose=versions.RUNTIME_CODE)
    fixed_time = int(time.time())
    source = root / "My_Function.py"
    os.utime(source, (fixed_time, fixed_time))
    py_compile.compile(str(source), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    runtime_cache = Path(importlib.util.cache_from_source(str(source)))
    config = root / "My_config.py"
    config.write_text("VALUE = 'current settings'\n", encoding="utf-8")
    py_compile.compile(str(config), doraise=True)
    config_cache = Path(importlib.util.cache_from_source(str(config)))
    kept_cache = config_cache.read_bytes()
    real_atomic = versions._atomic_bytes

    def same_timestamp(path, data):
        real_atomic(path, data)
        if Path(path).parent == root and Path(path).name in versions.RUNTIME_CODE:
            os.utime(path, (fixed_time, fixed_time))

    monkeypatch.setattr(versions, "_atomic_bytes", same_timestamp)
    versions.rollback(root, saved["id"])
    assert not runtime_cache.exists()
    assert config_cache.read_bytes() == kept_cache
    result = subprocess.run([sys.executable, "-I", "-c",
                             "import sys; sys.path.insert(0,sys.argv[1]); import My_Function; print(My_Function.VALUE)",
                             str(root)], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "old"
