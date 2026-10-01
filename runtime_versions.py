"""Local immutable snapshots and guarded rollback for Auto Report runtime code.

This module intentionally uses only the Python standard library so version
inspection remains available before the application dependencies are loaded.
"""
from __future__ import annotations

import contextlib
import ctypes
import datetime as _dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile


RUNTIME_CODE = (
    "Main.py", "Scheduler.py", "My_Function.py", "anomaly_engine.py",
    "operator_console.py", "resource_governor.py", "report_items.py",
)
REQUIRED_RUNTIME_CODE = tuple(name for name in RUNTIME_CODE if name != "report_items.py")
VERSION_RE = re.compile(r"^code-[0-9a-f]{16}$")


class RuntimeVersionError(RuntimeError):
    """Invalid, incomplete, or unsafe runtime version operation."""


class RuntimeLeaseError(RuntimeError):
    """A runtime lease is already held by an incompatible process."""


def _root_path(root):
    return Path(root).resolve()


def _store_path(root):
    override = os.environ.get("AUTO_REPORT_VERSION_STORE")
    path = Path(override).expanduser() if override else (_root_path(root) / ".runtime-versions")
    if not path.is_absolute():
        path = _root_path(root) / path
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise RuntimeVersionError(f"Version store may not be a symlink: {path}")
    except FileNotFoundError:
        pass
    return path.absolute()


def _regular_bytes(path, *, required=True):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeVersionError(f"Required runtime file is missing: {path}")
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeVersionError(f"Runtime/version path must be a regular file: {path}")
    return path.read_bytes()


def _effective_code(root):
    """Read each runtime member, preferring a loose source over its ZIP copy."""
    root = _root_path(root)
    loose = {}
    for name in RUNTIME_CODE:
        data = _regular_bytes(root / name, required=False)
        if data is not None:
            loose[name] = data
    zip_path = root / "auto_report_runtime.zip"
    zip_data = _regular_bytes(zip_path, required=False)
    zipped = {}
    if zip_data is not None:
        try:
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(zip_data), "r") as archive:
                names = set(archive.namelist())
                for name in RUNTIME_CODE:
                    if name in names:
                        info = archive.getinfo(name)
                        mode = info.external_attr >> 16
                        if stat.S_ISLNK(mode):
                            raise RuntimeVersionError(f"Runtime ZIP member may not be a symlink: {name}")
                        zipped[name] = archive.read(info)
        except RuntimeVersionError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise RuntimeVersionError(f"Cannot read runtime ZIP: {zip_path}") from exc
    code = {name: loose[name] if name in loose else zipped[name]
            for name in RUNTIME_CODE if name in loose or name in zipped}
    missing = [name for name in REQUIRED_RUNTIME_CODE if name not in code]
    if missing:
        raise RuntimeVersionError("Runtime code is incomplete: " + ", ".join(missing))
    return code, set(loose), zip_data is not None


def _content_id(code):
    digest = hashlib.sha256()
    for name in sorted(code):
        raw_name = name.encode("utf-8")
        data = code[name]
        digest.update(len(raw_name).to_bytes(4, "big")); digest.update(raw_name)
        digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return "code-" + digest.hexdigest()[:16]


def _hashes(code):
    return {name: hashlib.sha256(code[name]).hexdigest() for name in sorted(code)}


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISREG(mode)):
        raise RuntimeVersionError(f"Refusing to replace non-regular file: {path}")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, data)


@contextlib.contextmanager
def _registry_lock(store):
    """Short exclusive lock serializing snapshot metadata and events."""
    store.mkdir(parents=True, exist_ok=True)
    path = store / "registry.lock"
    if path.exists() and path.is_symlink():
        raise RuntimeVersionError(f"Registry lock may not be a symlink: {path}")
    stream = open(path, "a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeVersionError("Cannot acquire version registry lock") from exc
            try:
                yield
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


@contextlib.contextmanager
def runtime_lease(root, exclusive=False):
    """Hold a cross-process shared runtime reader or fail-fast exclusive writer lease."""
    store = _store_path(root)
    store.mkdir(parents=True, exist_ok=True)
    path = store / "runtime.lease"
    if path.exists() and path.is_symlink():
        raise RuntimeVersionError(f"Runtime lease may not be a symlink: {path}")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
            overlapped = _Overlapped()
            flags = 0x1 | (0x2 if exclusive else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel.LockFileEx(ctypes.c_void_p(handle), flags, 0, 1, 0,
                                     ctypes.byref(overlapped)):
                err = ctypes.get_last_error()
                if err in (32, 33, 158):
                    raise RuntimeLeaseError("Runtime is in use; version change refused")
                raise OSError(err, "LockFileEx failed")
            try:
                yield
            finally:
                if not kernel.UnlockFileEx(ctypes.c_void_p(handle), 0, 1, 0, ctypes.byref(overlapped)):
                    raise OSError(ctypes.get_last_error(), "UnlockFileEx failed")
        else:
            import fcntl
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeLeaseError("Runtime is in use; version change refused") from exc
                raise
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _manifest_for(code, version, label, created_at, loose_files, zip_present):
    return {"id": version, "label": str(label or ""), "created_at": created_at,
            "hashes": _hashes(code), "files": sorted(code),
            "loose_files": sorted(loose_files), "zip_present": bool(zip_present)}


def _append_event(store, event):
    path = store / "events.jsonl"
    if path.exists() and path.is_symlink():
        raise RuntimeVersionError(f"Version event log may not be a symlink: {path}")
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())


def snapshot(root, label=""):
    """Save the effective runtime code once and set it as the recorded current version."""
    root = _root_path(root)
    code, loose_files, zip_present = _effective_code(root)
    version = _content_id(code)
    store = _store_path(root)
    store.mkdir(parents=True, exist_ok=True)
    version_dir = store / version
    created_at = _utc_now()
    manifest = _manifest_for(code, version, label, created_at, loose_files, zip_present)
    with _registry_lock(store):
        if version_dir.exists():
            if version_dir.is_symlink():
                raise RuntimeVersionError(f"Version directory may not be a symlink: {version_dir}")
            _read_version(root, version)
            manifest = json.loads(_regular_bytes(version_dir / "manifest.json").decode("utf-8"))
        else:
            temp_dir = Path(tempfile.mkdtemp(prefix="." + version + ".", dir=str(store)))
            try:
                for name, data in code.items():
                    _atomic_bytes(temp_dir / name, data)
                _atomic_json(temp_dir / "manifest.json", manifest)
                os.replace(temp_dir, version_dir)
            finally:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
            _append_event(store, {"event": "snapshot", "id": version,
                                  "label": str(label or ""), "created_at": created_at})
        metadata = dict(manifest, status="current")
        _atomic_json(store / "current.json", metadata)
    return metadata


def _read_version(root, version):
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RuntimeVersionError("Invalid runtime version id")
    store = _store_path(root)
    version_dir = store / version
    if version_dir.is_symlink() or not version_dir.is_dir():
        raise RuntimeVersionError(f"Runtime version does not exist: {version}")
    manifest_path = version_dir / "manifest.json"
    try:
        manifest = json.loads(_regular_bytes(manifest_path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeVersionError(f"Invalid manifest for {version}") from exc
    if not isinstance(manifest, dict) or manifest.get("id") != version:
        raise RuntimeVersionError(f"Manifest id mismatch for {version}")
    files = manifest.get("files")
    hashes = manifest.get("hashes")
    if (not isinstance(files, list) or any(not isinstance(name, str) for name in files) or
            files != sorted(set(files)) or not set(REQUIRED_RUNTIME_CODE) <= set(files) <= set(RUNTIME_CODE) or
            not isinstance(hashes, dict) or set(hashes) != set(files)):
        raise RuntimeVersionError(f"Runtime version is incomplete: {version}")
    if (not isinstance(manifest.get("loose_files"), list) or
            any(not isinstance(name, str) for name in manifest["loose_files"]) or
            not set(manifest["loose_files"]) <= set(files)):
        raise RuntimeVersionError(f"Invalid source layout in {version}")
    if type(manifest.get("zip_present")) is not bool:
        raise RuntimeVersionError(f"Invalid ZIP source layout in {version}")
    code = {name: _regular_bytes(version_dir / name) for name in files}
    if _hashes(code) != manifest["hashes"] or _content_id(code) != version:
        raise RuntimeVersionError(f"Runtime version hash mismatch: {version}")
    return manifest, code


def current_version(root):
    """Describe effective on-disk code, whether or not it has been snapshotted."""
    code, loose_files, zip_present = _effective_code(root)
    version = _content_id(code)
    store = _store_path(root)
    label = ""
    created_at = None
    metadata = None
    version_dir = store / version
    if version_dir.exists():
        manifest, _ = _read_version(root, version)
        label = manifest.get("label", "")
        created_at = manifest.get("created_at")
        metadata = manifest
    recorded = None
    current_path = store / "current.json"
    if current_path.exists():
        try:
            recorded = json.loads(_regular_bytes(current_path).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeVersionError("Invalid current runtime metadata") from exc
    return {"id": version, "label": label, "created_at": created_at,
            "hashes": _hashes(code), "status": "current" if metadata else "unrecorded",
            "recorded_id": recorded.get("id") if isinstance(recorded, dict) else None,
            "loose_files": sorted(loose_files), "zip_present": zip_present}


def list_versions(root):
    """List verified immutable versions, newest first."""
    store = _store_path(root)
    if not store.exists():
        return []
    if store.is_symlink():
        raise RuntimeVersionError(f"Version store may not be a symlink: {store}")
    result = []
    for child in store.iterdir():
        if not VERSION_RE.fullmatch(child.name):
            continue
        manifest, _ = _read_version(root, child.name)
        result.append(manifest)
    return sorted(result, key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True)


def _make_runtime_zip(existing_zip, code):
    from io import BytesIO
    output = BytesIO()
    preserved = []
    if existing_zip is not None:
        try:
            with zipfile.ZipFile(BytesIO(existing_zip), "r") as archive:
                preserved = [(info, archive.read(info)) for info in archive.infolist()
                             if info.filename not in RUNTIME_CODE]
        except (OSError, zipfile.BadZipFile) as exc:
            raise RuntimeVersionError("Cannot preserve existing runtime ZIP") from exc
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, data in preserved:
            archive.writestr(info, data)
        for name in RUNTIME_CODE:
            if name not in code:
                continue
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, code[name])
    return output.getvalue()


def _discard_runtime_bytecode(root, names=RUNTIME_CODE):
    """Invalidate only owned runtime caches, including same-second replacements."""
    cache = root / "__pycache__"
    if cache.exists():
        info = cache.lstat()
        if (not stat.S_ISDIR(info.st_mode) or cache.is_symlink() or
                getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeVersionError(f"Runtime bytecode directory must not be a link: {cache}")
    for name in names:
        stem = Path(name).stem
        paths = [root / (stem + ".pyc")]
        if cache.is_dir():
            paths.extend(cache.glob(stem + ".*.pyc"))
        for path in paths:
            if path.exists():
                _regular_bytes(path)
                path.unlink()


def rollback(root, version):
    """Apply a verified snapshot under an exclusive lease; config and data are untouched."""
    root = _root_path(root)
    with runtime_lease(root, exclusive=True):
        target_manifest, target_code = _read_version(root, version)
        # Validate all sources before touching current code.
        for name, data in target_code.items():
            try:
                compile(data.decode("utf-8"), name, "exec")
            except (UnicodeError, SyntaxError) as exc:
                raise RuntimeVersionError(f"Version contains invalid Python: {name}") from exc
        previous_code, previous_loose, previous_zip_present = _effective_code(root)
        current_zip_path = root / "auto_report_runtime.zip"
        previous_zip = _regular_bytes(current_zip_path, required=False)
        # Snapshot current effective code before the change. This does not acquire
        # an exclusive lease, so it cannot deadlock inside the writer lease.
        before = snapshot(root, label="pre-rollback")
        desired_loose = set(target_manifest["loose_files"])
        desired_loose.update(("Main.py", "Scheduler.py"))
        desired_zip = None
        if target_manifest.get("zip_present") or previous_zip is not None:
            desired_zip = _make_runtime_zip(previous_zip, target_code)
        touched = []
        try:
            for name in RUNTIME_CODE:
                path = root / name
                if name in desired_loose:
                    old = _regular_bytes(path, required=False)
                    if old != target_code[name]:
                        _atomic_bytes(path, target_code[name]); touched.append(("write", path))
                elif path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeVersionError(f"Refusing to remove non-regular runtime source: {path}")
                    path.unlink(); touched.append(("remove", path))
            if desired_zip is not None:
                if previous_zip != desired_zip:
                    _atomic_bytes(current_zip_path, desired_zip); touched.append(("zip", current_zip_path))
            elif previous_zip is not None:
                current_zip_path.unlink(); touched.append(("remove_zip", current_zip_path))
            _discard_runtime_bytecode(root)
        except Exception as exc:
            restore_errors = []
            # Restore exact pre-apply source layout and ZIP after a partial write.
            for name in reversed(RUNTIME_CODE):
                path = root / name
                try:
                    if name in previous_loose:
                        _atomic_bytes(path, previous_code[name])
                    elif path.exists():
                        if path.is_symlink() or not path.is_file():
                            raise RuntimeVersionError(f"Refusing to remove non-regular runtime source: {path}")
                        path.unlink()
                except Exception as restore_exc:
                    restore_errors.append(f"{name}: {restore_exc}")
            try:
                if previous_zip is None:
                    if current_zip_path.exists():
                        current_zip_path.unlink()
                else:
                    _atomic_bytes(current_zip_path, previous_zip)
            except Exception as restore_exc:
                restore_errors.append(f"auto_report_runtime.zip: {restore_exc}")
            try:
                _discard_runtime_bytecode(root)
            except Exception as restore_exc:
                restore_errors.append(f"runtime bytecode: {restore_exc}")
            detail = f"Runtime rollback failed; previous version snapshot is {before['id']}: {exc}"
            if restore_errors:
                detail += "; restoration errors: " + "; ".join(restore_errors)
            raise RuntimeVersionError(detail) from exc
        store = _store_path(root)
        applied = dict(target_manifest, status="rolled_back")
        with _registry_lock(store):
            _atomic_json(store / "current.json", applied)
            _append_event(store, {"event": "rollback", "id": version,
                                  "from": before["id"], "created_at": _utc_now()})
        return applied
