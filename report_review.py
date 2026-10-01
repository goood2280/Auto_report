#!/usr/bin/env python3
"""Isolated candidate review workflow for Auto Report installations.

This tool is intentionally local. A candidate is a sibling copy with its own
RUN tree; only explicitly named input files are copied from an installation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import html.parser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

_runtime_zip = str(Path(__file__).resolve().parent / 'auto_report_runtime.zip')
if Path(_runtime_zip).is_file() and _runtime_zip not in sys.path:
    sys.path.append(_runtime_zip)


CODE_FILES = (
    "Main.py", "Scheduler.py", "My_config.py", "My_Function.py",
    "anomaly_engine.py", "operator_console.py", "resource_governor.py", "report_items.py",
    "report_review.py", "runtime_versions.py", "auto_report_runtime.zip", "gen_setup.py",
)
COPY_FILES = CODE_FILES + ("gen_setup.py", "setup.py")
DOC_FILES = ("AGENTS.md", "README.md", "CLAUDE.md", "ANOMALY_KNOWLEDGE.md")
ROOT_ASSETS = {
    "HOL_Auto_Report_Description.pptx", "HOL_Auto_Report_Mailing_List.xlsx",
    "INLINE_1_reformatter.xlsx", "SF3_Data_Extractor_Input_File_v0.xlsx",
}
INPUT_ROOTS = ("reformatter", "RUN/DB")
MANIFEST = ".report-review.json"
REVIEW_DIR = ".review"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _owned_hashes(root: Path) -> dict[str, str]:
    return {name: _sha(root / name) for name in CODE_FILES if (root / name).is_file()}


def _control_hashes(root: Path) -> dict[str, str]:
    paths = set(DOC_FILES)
    paths.update(("reformatter/config.yaml", "reformatter/scheduler.yaml",
                  "report_items.yaml", "reformatter/report_items.yaml"))
    formatter_dir = root / "reformatter"
    if formatter_dir.is_dir():
        paths.update(p.relative_to(root).as_posix() for p in formatter_dir.glob("*_reformatter.csv") if p.is_file())
    paths.update(ROOT_ASSETS)
    for folder in ("docs", "tests"):
        base = root / folder
        if base.is_dir():
            paths.update(p.relative_to(root).as_posix() for p in base.rglob("*")
                         if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts)
    result = {}
    for rel in sorted(paths):
        path = root / rel
        if path.is_file() and not _is_link_or_junction(path):
            result[rel] = _sha(path)
    return result


def _source_baseline_hashes(root: Path) -> dict[str, str]:
    return {**_owned_hashes(root), **_control_hashes(root),
            **({'setup.py': _sha(root / 'setup.py')} if (root / 'setup.py').is_file() else {})}


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        import stat
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _selection_hash(root: Path) -> str:
    paths = [root / "report_items.yaml", root / "reformatter" / "report_items.yaml"]
    payload = [(p.relative_to(root).as_posix(), _sha(p)) for p in paths if p.is_file()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _fingerprint(root: Path) -> str:
    marker = root / MANIFEST
    inputs = json.loads(marker.read_text(encoding='utf-8')).get('input_files', []) if marker.is_file() else []
    db = root / 'RUN' / 'DB'
    sample_db = {}
    if db.is_dir():
        for path in db.rglob('*'):
            if path.is_file() and (path.name.startswith('ML_TABLE_') or path.name.endswith('_wip_current.csv')
                                   or (path.name == 'data.parquet' and path.parent.name.startswith('date='))):
                info = path.stat()
                sample_db[path.relative_to(root).as_posix()] = [info.st_size, info.st_mtime_ns]
    body = {"code": _owned_hashes(root), "controls": _control_hashes(root), 'sample_db': sample_db,
            "report_items": _selection_hash(root),
            "inputs": {name: _sha(root / name) if (root / name).is_file() else None for name in inputs}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _candidate_root() -> Path:
    root = Path(__file__).resolve().parent
    marker = root / MANIFEST
    if not marker.is_file():
        raise ValueError("This command must run from a prepared candidate folder")
    data = json.loads(marker.read_text(encoding="utf-8"))
    if Path(data.get("target", "")).resolve() != root:
        raise ValueError("Candidate marker does not match this folder")
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / MANIFEST).read_text(encoding="utf-8"))


def _safe_relative(source: Path, rel: str, *, allow_log=False) -> Path:
    relpath = Path(rel)
    if relpath.is_absolute() or relpath.drive or relpath.anchor or not relpath.parts or any(p in (".", "..") for p in relpath.parts):
        raise ValueError(f"Input must be a safe relative path: {rel}")
    candidate = source / relpath
    cur = source
    for part in relpath.parts:
        cur = cur / part
        if _is_link_or_junction(cur):
            raise ValueError(f"Symlink inputs are not allowed: {rel}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(source.resolve()):
        raise ValueError(f"Input escapes source folder: {rel}")
    valid = relpath.parts[0] == "reformatter" or relpath.parts[:2] == ("RUN", "DB")
    if allow_log and len(relpath.parts) == 3 and relpath.parts[:2] == ("RUN", "log"):
        valid = relpath.name.endswith("_et_log.csv") or bool(re.fullmatch(r".+_et_log.*\.csv", relpath.name, re.I))
    if len(relpath.parts) == 1 and relpath.name in ROOT_ASSETS:
        valid = True
    if not valid or not (resolved.is_file() or resolved.is_dir()):
        raise ValueError(f"Input is outside the explicit input allowlist: {rel}")
    return resolved


def _expand_input(source: Path, rel: str) -> list[tuple[Path, Path]]:
    base = _safe_relative(source, rel, allow_log=True)
    relpath = Path(rel)
    if base.is_file():
        if base.name.lower() == ".env":
            raise ValueError(".env is never copied into a candidate")
        return [(base, relpath)]
    results = []
    for current, dirs, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        for name in dirs + files:
            child = current_path / name
            if _is_link_or_junction(child):
                raise ValueError(f"Symlink or junction inputs are not allowed: {child.relative_to(source)}")
            if name.lower() == ".env":
                raise ValueError(".env is never copied into a candidate")
        for name in files:
            child = current_path / name
            child_rel = child.relative_to(source)
            parts = child_rel.parts
            allowed = parts[0] == "reformatter" or parts[:2] == ("RUN", "DB")
            if parts[:2] == ("RUN", "log"):
                allowed = len(parts) == 3 and bool(re.fullmatch(r".+_et_log.*\.csv", name, re.I))
            if allowed and child.is_file():
                results.append((child, child_rel))
    return results


def prepare(source: str, target: str, inputs: list[str]) -> Path:
    src = Path(source).resolve(strict=True)
    dst = Path(target).absolute()
    if not src.is_dir():
        raise ValueError("Source must be an installation folder")
    if dst.exists():
        raise ValueError("Target must be a new folder")
    target_parent = dst.parent.resolve(strict=True)
    dst = target_parent / dst.name
    if dst == src or src.is_relative_to(dst) or dst.is_relative_to(src):
        raise ValueError("Target must be a separate sibling folder, outside and not an ancestor of source")
    dst.mkdir()
    try:
        for name in COPY_FILES:
            source_file = src / name
            if source_file.is_file() and not _is_link_or_junction(source_file):
                shutil.copy2(source_file, dst / name)
        for name in DOC_FILES:
            source_file = src / name
            if source_file.is_file() and not _is_link_or_junction(source_file):
                shutil.copy2(source_file, dst / name)
        for folder in ("docs", "tests"):
            origin = src / folder
            if origin.is_dir() and not _is_link_or_junction(origin):
                for current, dirs, files in os.walk(origin, followlinks=False):
                    for name in dirs + files:
                        item = Path(current) / name
                        if _is_link_or_junction(item):
                            raise ValueError(f"Candidate source tree contains a link or junction: {item.relative_to(src)}")
                shutil.copytree(origin, dst / folder, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        copied = set()
        input_files = []
        for rel in inputs:
            for file, relative in _expand_input(src, rel):
                key = relative.as_posix().lower()
                if key in copied:
                    continue
                copied.add(key)
                dest = dst / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, dest)
                input_files.append(relative.as_posix())
        required = ("setup.py", "Main.py", "Scheduler.py", "My_config.py")
        missing = [name for name in required if not (dst / name).is_file()]
        if missing:
            raise ValueError("Source is missing required install files: " + ", ".join(missing))
        # Extract only candidate-owned helper source files when the compact install lacks them.
        helper_names = ("My_Function.py", "anomaly_engine.py", "operator_console.py", "resource_governor.py", "report_items.py", "runtime_versions.py")
        if any(not (dst / name).is_file() for name in helper_names) or not (dst / 'tests').is_dir():
            subprocess.run([sys.executable, "setup.py", "--extract-sources"], cwd=dst, check=True)
        if any(not (dst / name).is_file() for name in helper_names):
            raise ValueError("Candidate is missing editable helper sources after extraction")
        runtime_zip = dst / "auto_report_runtime.zip"
        if not runtime_zip.is_file():
            with zipfile.ZipFile(runtime_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in helper_names:
                    archive.write(dst / name, name)
        manifest = {
            "schema": "auto_report.candidate/1", "source": str(src), "target": str(dst.resolve()),
            "baseline": _source_baseline_hashes(src), "created_at": __import__("datetime").datetime.now().astimezone().isoformat(),
            "inputs": list(inputs),
            "input_files": sorted(input_files),
        }
        _json_write(dst / MANIFEST, manifest)
        return dst
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def _latest_check(root: Path) -> dict:
    path = root / REVIEW_DIR / "check.json"
    if not path.is_file():
        raise ValueError("Run report_review.py check with passing explicit tests first")
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("status") != "passed" or result.get("fingerprint") != _fingerprint(root):
        raise ValueError("Passing check is stale or failed; rerun the declared tests")
    return result


def check(test_paths: list[str]) -> int:
    root = _candidate_root()
    if not test_paths:
        raise ValueError("Declare at least one test file with --test")
    tests = []
    for raw in test_paths:
        p = Path(raw)
        if p.is_absolute() or len(p.parts) != 2 or p.parts[0] != "tests" or p.suffix != ".py" or ".." in p.parts:
            raise ValueError("Only explicit tests/NAME.py paths are allowed")
        path = root / p
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Test file missing or symlinked: {raw}")
        tests.append(p.as_posix())
    review_tmp = root / REVIEW_DIR / "test-tmp"
    env = os.environ.copy()
    env.update({
        "AUTO_REPORT_OPS_ROOT": str(review_tmp / "ops"),
        "AUTO_REPORT_TEMP_DIR": str(review_tmp / "temp"),
        "AUTO_REPORT_SLOT_DIR": str(review_tmp / "slots"),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONIOENCODING": "utf-8",
        'AUTO_REPORT_VERSION_STORE': str(review_tmp / 'versions'),
    })
    for key in ("AUTO_REPORT_EMAIL_RECEIVER", "AUTO_REPORT_REQUEST_ID", "AUTO_REPORT_RUN_ID",
                "AUTO_REPORT_RESULT_PATH", "AUTO_REPORT_GENERATE_ONLY"):
        env.pop(key, None)
    for value in ("ops", "temp", "slots"):
        (review_tmp / value).mkdir(parents=True, exist_ok=True)
    before = _fingerprint(root)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", '-p', 'no:cacheprovider',
                           '--basetemp', str(review_tmp / 'pytest'), *tests], cwd=root, env=env)
    after = _fingerprint(root)
    changed = before != after
    returncode = proc.returncode or (2 if changed else 0)
    payload = {"status": "passed" if returncode == 0 else "failed", "tests": tests,
               "returncode": returncode, "fingerprint": after, "source_changed_during_check": changed,
               "report_items_hash": _selection_hash(root)}
    _json_write(root / REVIEW_DIR / "check.json", payload)
    if changed:
        print("Candidate code or inputs changed during check; rerun tests", file=sys.stderr)
    return returncode


class _HTMLProbe(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.images += 1
            if not dict(attrs).get('src', '').startswith('data:image/'):
                raise ValueError('Review HTML images must use inline data:image sources')


def record(html_file: str, ppt_file: str, vehicle: str, title: str) -> str:
    root = _candidate_root()
    _latest_check(root)
    files = []
    for raw in (html_file, ppt_file):
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if path.is_symlink():
            raise ValueError("Review artifacts must be candidate-owned regular files")
        path = path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Review artifacts must be candidate-owned regular files")
        files.append(path)
    html_path, ppt_path = files
    content = html_path.read_text(encoding="utf-8")
    if "<html" not in content.lower() or "</html>" not in content.lower():
        raise ValueError("HTML artifact must contain an html document")
    probe = _HTMLProbe()
    probe.feed(content)
    if probe.images + 1 > 10:
        raise ValueError('Review sample exceeds the mail attachment/image count; register a split part')
    with zipfile.ZipFile(ppt_path) as archive:
        names = set(archive.namelist())
        if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
            raise ValueError("PPT artifact is not a valid PowerPoint package")
        if archive.testzip() is not None:
            raise ValueError("PPT artifact contains a corrupt ZIP member")
    review_id = uuid.uuid4().hex
    receipt = {"schema": "auto_report.review/1", "review_id": review_id,
               "vehicle": vehicle, "title": title, "created_at": __import__("datetime").datetime.now().astimezone().isoformat(),
               "fingerprint": _fingerprint(root), "report_items_hash": _selection_hash(root),
               "html": {"path": html_path.relative_to(root).as_posix(), "sha256": _sha(html_path)},
               "ppt": {"path": ppt_path.relative_to(root).as_posix(), "sha256": _sha(ppt_path)}}
    _json_write(root / REVIEW_DIR / "receipts" / f"{review_id}.json", receipt)
    return review_id


def _read_receipt(root: Path, review_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", review_id):
        raise ValueError("Invalid review ID")
    path = root / REVIEW_DIR / "receipts" / f"{review_id}.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    _latest_check(root)
    if receipt.get("fingerprint") != _fingerprint(root) or receipt.get("report_items_hash") != _selection_hash(root):
        raise ValueError("Review receipt is stale: candidate code or selected report items changed")
    for key in ("html", "ppt"):
        item = receipt[key]
        artifact = root / item["path"]
        if artifact.is_symlink() or not artifact.is_file() or not artifact.resolve().is_relative_to(root):
            raise ValueError("Frozen review artifact is missing or outside the candidate")
        if _sha(artifact) != item["sha256"]:
            raise ValueError("Frozen review artifact has changed")
    return receipt


def _isolated_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    slot_dir = env.get('AUTO_REPORT_SLOT_DIR')
    for key in list(env):
        if key.startswith("AUTO_REPORT_"):
            env.pop(key, None)
    env.update({
        "AUTO_REPORT_OPS_ROOT": str(root / "RUN" / "OPS"),
        "AUTO_REPORT_TEMP_DIR": str(root / "RUN" / "TEMP"),
        "AUTO_REPORT_GENERATE_ONLY": "1",
        "AUTO_REPORT_EXECUTION_WAIT_SEC": "0",
        'AUTO_REPORT_VERSION_STORE': str(root / '.runtime-versions'),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    if slot_dir:
        env['AUTO_REPORT_SLOT_DIR'] = slot_dir
    return env


def _validate_candidate_config(config, root: Path) -> None:
    if Path(config.base_path).resolve() != root.resolve():
        raise ValueError("Config base path is outside candidate")
    path_keys = ("inline_file_path", "coordinate_file_path", "description_ppt_path",
                 "anomaly_knowledge_path", "email_list_path", "ml_table_dir",
                 "items_file", "DB", "DB_et_daily", "Report", "log", "ROOT",
                 "html_save_path", "low_qual_ppt_save_path", "et_log_path", "Final_et_log_path",
                 'query_log', 'loop_log', 'error_log', 'unified_log', 'running_log', 'run_temp_dir')
    for key in path_keys:
        value = config.get(key)
        if not value:
            continue
        resolved = Path(str(value))
        if not resolved.is_absolute():
            resolved = root / resolved
        if not resolved.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Config path {key} escapes the candidate folder")


def preview(vehicle: str, lot: str | None = None, step: str | None = None,
            service: str = "auto") -> int:
    root = _candidate_root()
    if service not in ("auto", "daily_trend", "mlmode"):
        raise ValueError("service must be auto, daily_trend, or mlmode")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", vehicle):
        raise ValueError("Invalid vehicle key")
    if service == "auto":
        if not lot or not step or not re.fullmatch(r"[A-Za-z0-9.-]{1,40}", lot) or not re.fullmatch(r"[A-Za-z0-9.-]{1,40}", step):
            raise ValueError("auto preview requires safe --lot and --step values")
    elif lot or step:
        raise ValueError("--lot/--step apply only to auto preview")
    env = _isolated_environment(root)
    proc = subprocess.run([sys.executable, str(root / "report_review.py"), "_preview_worker",
                           "--vehicle", vehicle, "--service", service,
                           *( ["--lot", lot, "--step", step] if lot and step else [])],
                          cwd=root, env=env)
    return proc.returncode


def _preview_worker(vehicle: str, service: str, lot: str | None, step: str | None) -> int:
    root = _candidate_root()
    clean_env = _isolated_environment(root)
    for key in list(os.environ):
        if key.startswith('AUTO_REPORT_') and key not in clean_env:
            os.environ.pop(key)
    os.environ.update(clean_env)
    import Main as main

    config = main.GLOBAL_CONFIG
    original_loader = config.load_from_yaml

    def candidate_loader(name, yaml_path=None):
        if yaml_path is not None and not Path(yaml_path).resolve().is_relative_to(root):
            raise ValueError("Config YAML path escapes candidate")
        result = original_loader(name, yaml_path)
        _validate_candidate_config(config, root)
        config.settings.update(use_email_send=False, use_s3_upload=False, test_mode=True)
        config.settings["DB_Setting_mode"] = False
        return result

    config.load_from_yaml = candidate_loader
    if service == "auto":
        import pandas as pd
        config.load_from_yaml(vehicle)
        config.settings["test_mode"] = True
        # TRIGGER reads only candidate DB/ET/WIP inputs. Inline data is intentionally empty;
        # these guards make an attempted source-system query fail closed.
        main.etdata_query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ET query disabled in candidate preview"))
        main.wipdata_query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("WIP query disabled in candidate preview"))
        main.inlinedata_query = lambda *a, **k: pd.DataFrame()
        arg = f"_TRIGGER_{vehicle}_{lot}_{step}"
        previous_args = sys.argv
        try:
            sys.argv = [str(root / 'Main.py'), arg]
            rc = main.main()
        finally:
            sys.argv = previous_args
        if rc or not main._RUN.data.get('reports'):
            raise ValueError('Auto Report preview did not generate a report; check local inputs/target')
        return rc

    import Scheduler as scheduler
    settings = scheduler.load_config(create_if_missing=False, strict_services=False)[service]
    settings = dict(settings)
    settings.update(products=[vehicle], recipients=[], enabled=False,
                    ml_table_dir=str(root / "RUN" / "DB"), send=False)
    if settings.get("items_file"):
        items = Path(settings["items_file"])
        if not items.is_absolute():
            items = root / items
        if not items.resolve().is_relative_to(root):
            raise ValueError("Report item configuration escapes candidate")
        settings["items_file"] = str(items)
    # Validate product paths before Daily/ML reads its DB, formatter, or coordinate files.
    config.load_from_yaml(vehicle)
    request = dict(id="preview-" + uuid.uuid4().hex, settings=settings,
                   now=__import__("time").time(), service=service, send=False)
    from runtime_versions import runtime_lease, snapshot
    with runtime_lease(root):
        main._CODE_VERSION = snapshot(root)['id']
        print('[VERSION] Auto Report ' + main._CODE_VERSION, flush=True)
        result = main._execute_serially(lambda: main._daily_trend_report(request))
    summary = {key: result[key] for key in ('id', 'status', 'code_version', 'items', 'manifest', 'coverage', 'reason') if key in result}
    summary['parts'] = [{key: part[key] for key in ('html', 'ppt', 'count')} for part in result.get('parts', [])]
    print(json.dumps(summary, ensure_ascii=False, default=str))
    if result.get("status") == "skipped":
        raise ValueError(result.get("reason", "Daily/ML preview produced no report"))
    return 0


def _send_sample(review_id: str, users: list[str]) -> int:
    root = _candidate_root()
    if not users:
        raise ValueError("Specify at least one selected user ID")
    receipt = _read_receipt(root, review_id)
    # Prevent dotenv discovery from loading a nearby installation's credentials.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["AUTO_REPORT_OPS_ROOT"] = str(root / "RUN" / "OPS")
    os.environ["AUTO_REPORT_TEMP_DIR"] = str(root / "RUN" / "TEMP")
    os.environ.pop("AUTO_REPORT_EMAIL_RECEIVER", None)
    if Path(__file__).resolve().parent != root:
        raise ValueError("Refusing to import report code outside candidate")
    import Main as main  # candidate-local code only
    import My_Function as mf
    if Path(main.__file__).resolve().parent != root or Path(mf.__file__).resolve().parent not in (root, root / 'auto_report_runtime.zip'):
        raise ValueError("Refusing to send from code outside candidate")
    main.GLOBAL_CONFIG.load_from_yaml(receipt["vehicle"])
    _validate_candidate_config(main.GLOBAL_CONFIG, root)
    config = main.GLOBAL_CONFIG
    config.settings["use_s3_upload"] = False
    html_path = root / receipt["html"]["path"]
    ppt_path = root / receipt["ppt"]["path"]
    probe = _HTMLProbe()
    probe.feed(html_path.read_text(encoding='utf-8'))
    if probe.images + 1 > int(config.get('mail_attach_limit', 10)):
        raise ValueError('Frozen sample exceeds the configured mail image count; generate/register split parts')
    statuses = []
    validated = [(user, mf.samsung_email(user)) for user in dict.fromkeys(u.strip().lower() for u in users)]
    for user, address in validated:
        recipients = mf.email_receivers([address])
        identity = f"candidate-review:{review_id}:{user.strip().lower()}"
        send_title = "TEST · " + receipt["title"]
        status = main._durable_mail(identity, recipients, send_title, str(html_path), str(ppt_path), config)
        statuses.append(status)
        _json_write(root / REVIEW_DIR / "sends" / f"{review_id}-{user.strip().lower()}.json",
                    {"review_id": review_id, "user": user, "status": status, "title": send_title})
        print(json.dumps({'review_id':review_id, 'user':user, 'status':status}, ensure_ascii=False))
    if all(status == "sent" for status in statuses):
        return 0
    return 2


def send_sample(review_id: str, users: list[str]) -> int:
    if not re.fullmatch(r'[a-f0-9]{32}', review_id):
        raise ValueError('Invalid review ID')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    from runtime_versions import runtime_lease
    from My_Function import process_lock
    root = _candidate_root()
    with runtime_lease(root), process_lock(str(root / 'RUN' / 'OPS' / 'locks' / ('sample-' + review_id + '.lock'))):
        return _send_sample(review_id, users)


def promotion_plan(review_id: str) -> dict:
    root = _candidate_root()
    receipt = _read_receipt(root, review_id)
    manifest = _manifest(root)
    source = Path(manifest["source"]).resolve(strict=True)
    current = _source_baseline_hashes(source)
    if current != manifest["baseline"]:
        raise ValueError("Production source drifted since candidate preparation; prepare a fresh candidate")
    subprocess.run([sys.executable, "setup.py", "--build"], cwd=root, check=True)
    setup = root / "setup.py"
    plan = {"review_id": review_id, "source": str(source), "candidate": str(root),
            "target": str(source), "setup_sha256": _sha(setup),
            "changed_owned_files": sorted(name for name, digest in _owned_hashes(root).items()
                                           if manifest["baseline"].get(name) != digest),
            "report_items_hash": receipt["report_items_hash"],
            "manual_steps": ["Review this plan and the candidate bundle.",
                             "Install the candidate setup.py bundle into the active runtime using the approved deployment procedure.",
                             "Apply report_items.yaml explicitly if it is part of the approved selection change.",
                             "Verify installation backups, then restart only through the deployment account."]}
    _json_write(root / REVIEW_DIR / f"promotion-{review_id}.json", plan)
    return plan


def item_command(args):
    import pandas as pd
    from report_items import load_catalog, select_formatter, update_items
    root = Path(__file__).resolve().parent
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', args.vehicle):
        raise ValueError('Invalid vehicle key')
    formatter = pd.read_csv(root / 'reformatter' / (args.vehicle + '_reformatter.csv'))
    path = root / 'reformatter' / 'report_items.yaml'
    services = ['daily_trend', 'mlmode'] if args.service == 'both' else [args.service]
    if args.command != 'items-list':
        _candidate_root()
        for service in services:
            options = None
            if args.command == 'items-add' and (args.time_column is not None or args.split_column is not None):
                _, previous = select_formatter(formatter, args.vehicle, service, load_catalog(path))
                options = {}
                for alias in args.item:
                    value = dict(previous.get(alias, {'time_column': '', 'split_columns': []}))
                    if args.time_column is not None:
                        value['time_column'] = args.time_column
                    if args.split_column is not None:
                        value['split_columns'] = args.split_column
                    options[alias] = value
            update_items(path, formatter, args.vehicle, service,
                         add=args.item if args.command == 'items-add' else [],
                         remove=args.item if args.command == 'items-remove' else [], options=options)
    catalog = load_catalog(path)
    return {service: select_formatter(formatter, args.vehicle, service, catalog)[1] for service in services}


def rollback_version(version, config_path=None):
    """Hold legacy locks too; newer processes also hold the shared runtime lease."""
    import Scheduler as scheduler
    import My_Function as mf
    from runtime_versions import rollback
    root = Path(__file__).resolve().parent
    cfg = scheduler.load_config(config_path or str(root / 'reformatter' / 'scheduler.yaml'),
                                create_if_missing=False, strict_services=False)
    queue = Path(scheduler._abspath(cfg['trigger'].get('queue_root', 'RUN/QUEUE')))
    ops = Path(scheduler._ops_root(cfg))
    guards = [queue / 'scheduler.lock.guard']
    guards.extend(ops / (service + '.lock.guard') for service in ('watchdog', 'daily_trend', 'mlmode'))
    guards.extend((ops / 'locks').glob('*.lock'))
    with ExitStack() as stack:
        for path in guards:
            if path.is_file():
                stack.enter_context(mf.process_lock(str(path), wait_sec=0))
        return rollback(root, version)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and review an isolated Auto Report candidate")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="copy an install folder into a new candidate sibling")
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--input", action="append", default=[], metavar="RELATIVE")
    p = commands.add_parser("check", help="run only explicitly declared offline tests")
    p.add_argument("--test", action="append", default=[], required=True, metavar="tests/NAME.py")
    p = commands.add_parser("record", help="freeze candidate HTML/PPT for review")
    p.add_argument("--html", required=True)
    p.add_argument("--ppt", required=True)
    p.add_argument("--vehicle", required=True)
    p.add_argument("--title", required=True)
    p = commands.add_parser("send-sample", help="send frozen candidate artifacts to selected IDs")
    p.add_argument("--review", required=True)
    p.add_argument("--user", action="append", required=True, metavar="DOMAINLESS_ID")
    p = commands.add_parser("promotion-plan", help="build candidate installer and emit a manual promotion plan")
    p.add_argument("--review", required=True)
    p = commands.add_parser("preview", help="generate candidate-only Auto Report, Daily Trend, or ML artifacts")
    p.add_argument("--vehicle", required=True)
    p.add_argument("--lot")
    p.add_argument("--step")
    p.add_argument("--service", choices=("auto", "daily_trend", "mlmode"), default="auto")
    worker = commands.add_parser("_preview_worker", help=argparse.SUPPRESS)
    worker.add_argument("--vehicle", required=True)
    worker.add_argument("--lot")
    worker.add_argument("--step")
    worker.add_argument("--service", choices=("auto", "daily_trend", "mlmode"), required=True)
    for name in ('items-list', 'items-add', 'items-remove'):
        p = commands.add_parser(name, help='list/edit product-specific report item dict')
        p.add_argument('--vehicle', required=True)
        p.add_argument('--service', choices=('daily_trend', 'mlmode', 'both'), required=True)
        if name != 'items-list':
            p.add_argument('--item', action='append', required=True)
        if name == 'items-add':
            p.add_argument('--time-column')
            p.add_argument('--split-column', action='append')
    commands.add_parser('current-version', help='show effective runtime code version (read only)')
    commands.add_parser('versions', help='list saved local code versions (read only)')
    p = commands.add_parser('save-version', help='save current effective code; keep settings separately')
    p.add_argument('--label', default='')
    p = commands.add_parser('rollback', help='restore saved code while preserving current configuration')
    p.add_argument('--version', required=True)
    p.add_argument('--config', help='current Scheduler YAML used for legacy lock checks')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare(args.source, args.target, args.input))
            return 0
        if args.command == "check":
            return check(args.test)
        if args.command == "record":
            print(record(args.html, args.ppt, args.vehicle, args.title))
            return 0
        if args.command == "send-sample":
            return send_sample(args.review, args.user)
        if args.command == "preview":
            return preview(args.vehicle, args.lot, args.step, args.service)
        if args.command == "_preview_worker":
            return _preview_worker(args.vehicle, args.service, args.lot, args.step)
        if args.command.startswith('items-'):
            print(json.dumps(item_command(args), ensure_ascii=False, indent=2))
            return 0
        if args.command in ('current-version', 'versions', 'save-version', 'rollback'):
            import runtime_versions as versions
            root = Path(__file__).resolve().parent
            if args.command == 'current-version':
                result = versions.current_version(root)
            elif args.command == 'versions':
                result = versions.list_versions(root)
            elif args.command == 'save-version':
                with versions.runtime_lease(root):
                    result = versions.snapshot(root, label=args.label)
            else:
                result = rollback_version(args.version, args.config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        plan = promotion_plan(args.review)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"report_review: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
