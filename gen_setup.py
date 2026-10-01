#!/usr/bin/env python3
"""Build the self-contained installer from editable files or the compact runtime ZIP."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import textwrap
import zipfile

RUNTIME_FILES = ['My_Function.py', 'anomaly_engine.py', 'operator_console.py', 'resource_governor.py', 'report_items.py', 'runtime_versions.py']
ENTRY_FILES = ['Main.py', 'Scheduler.py', 'My_config.py', 'report_review.py']
TEST_FILES = [
    'tests/test_compact_setup.py', 'tests/test_daily_services.py', 'tests/test_db_setting.py',
    'tests/test_execution_gate.py', 'tests/test_installer_retired_cleanup.py', 'tests/test_ml_insight.py',
    'tests/test_operator_commands.py', 'tests/test_parallel_multi.py', 'tests/test_report_size.py',
    'tests/test_scheduler_queue.py', 'tests/test_score_single.py', 'tests/test_report_items.py',
    'tests/test_report_review.py', 'tests/test_runtime_versions.py',
]
BUNDLE_FILES = ENTRY_FILES + RUNTIME_FILES + [
    'gen_setup.py', 'AGENTS.md', 'CLAUDE.md', 'ANOMALY_KNOWLEDGE.md', 'README.md',
    'docs/SCHEDULER_TRIGGER_CONTRACT.md',
    'docs/REPORT_REVIEW.md',
    'docs/guide/index.html', 'docs/guide/auto-report-architecture.md',
    'docs/guide/01-overview.svg', 'docs/guide/01-overview.mmd',
    'docs/guide/02-runtime.svg', 'docs/guide/02-runtime.mmd',
    'docs/guide/03-lot-pipeline.svg', 'docs/guide/03-lot-pipeline.mmd',
    'docs/guide/04-trend-ml.svg', 'docs/guide/04-trend-ml.mmd',
    'docs/guide/05-delivery.svg', 'docs/guide/05-delivery.mmd',
] + TEST_FILES
RETIRED_FILES = [
    'Manager.py', 'manager.html', 'manager-reader.js', 'manager-app.js', 'manager-tune.js',
    'manager_llm.py', 'manager_assistant.py', 'manager_explain.py', 'ml_threshold_tuner.py',
    'gpt_oss_client.py', 'docs/MANAGER_START.md',
]

INSTALLER = r'''
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import zipfile


def read_bundle():
    compressed = base64.b64decode(DATA)
    if hashlib.sha256(compressed).hexdigest() != CHECKSUM:
        raise ValueError('Bundle checksum mismatch; installation stopped')
    return json.loads(gzip.decompress(compressed).decode('utf-8'))


def _write(path, data, backup_root, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    # Prepare complete replacement before moving the old file; backups must succeed.
    fd, temp = tempfile.mkstemp(prefix='._setup_', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        saved = None
        if path.exists():
            saved = backup_root / (path.relative_to(root).as_posix() + '.bak')
            saved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, saved)
        try:
            os.replace(temp, path)
        except OSError:
            if saved is not None:
                os.replace(saved, path)
            raise
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0)
                                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _cleanup_retired_artifacts(root):
    """Remove exact retired bytecode and generated AI files; never traverse links or operational data."""
    removed = 0
    modules = {Path(name).stem for name in RETIRED_FILES if name.endswith('.py')}
    for directory in (root, root / '__pycache__'):
        if not directory.is_dir() or _is_link(directory):
            continue
        for path in directory.glob('*.pyc'):
            if path.name.split('.', 1)[0] in modules and path.is_file() and not _is_link(path):
                path.unlink()
                removed += 1
    run, ai = root / 'RUN', root / 'RUN' / 'AI'
    if not run.is_dir() or _is_link(run) or not ai.is_dir() or _is_link(ai):
        return removed
    pattern = (r'(?:ai_input_.+\.(?:md|json)|anomaly_rule_check_.+\.(?:txt|json)|'
               r'nl_rules_(?:json|compiled|map)\.json|rule_digest_\d{8}\.txt|rule_digest_state\.json)')
    for path in ai.iterdir():
        if re.fullmatch(pattern, path.name) and path.is_file() and not _is_link(path):
            path.unlink()
            removed += 1
    try:
        ai.rmdir()  # Preserve unknown files or directories; only an empty AI directory is removed.
    except OSError:
        pass
    return removed


def _install_payload(target_dir=None, sources=False, preserve_config=False):
    bundle = read_bundle()
    root = Path(target_dir or Path(__file__).resolve().parent).resolve()
    root.mkdir(parents=True, exist_ok=True)
    backup_root = root / '.setup-backups' / uuid.uuid4().hex
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as out:
        for name in RUNTIME_FILES:
            item = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(item, bundle[name].encode('utf-8'))
    _write(root / 'auto_report_runtime.zip', archive.getvalue(), backup_root, root)
    for name, content in bundle.items():
        if name in RUNTIME_FILES or name == 'gen_setup.py' or name.startswith('tests/'):
            continue
        if name == 'My_config.py' and preserve_config and (root / name).is_file():
            continue
        _write(root / name, content.encode('utf-8'), backup_root, root)
    # Exact owned filenames only. Preserve removed/loose sources as .bak; keep config YAML and operational data.
    obsolete = RETIRED_FILES + RUNTIME_FILES + ['gen_setup.py']
    for name in obsolete:
        source = root / name
        if source.is_file():
            saved = backup_root / (name + '.bak')
            saved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, saved)
    removed = _cleanup_retired_artifacts(root)
    if sources:
        extract_sources(root)
    print('[setup] Installed:', root)
    print('[setup] Python entry files: Main.py, Scheduler.py, My_config.py, report_review.py (4; setup.py excluded)')
    print('[setup] Runtime source: auto_report_runtime.zip; instructions: AGENTS.md')
    if backup_root.exists():
        print('[setup] Previous files:', backup_root)
    if removed:
        print('[setup] Retired AI artifacts removed:', removed)
    print('[setup] Restart existing processes to load this release.')


def install(target_dir=None, sources=False, preserve_config=False):
    bundle = read_bundle()
    root = Path(target_dir or Path(__file__).resolve().parent).resolve()
    scope = {'__name__': '_auto_report_versions', '__file__': str(root / 'runtime_versions.py')}
    exec(compile(bundle['runtime_versions.py'], scope['__file__'], 'exec'), scope)
    kept_config = preserve_config and (root / 'My_config.py').is_file()
    with scope['runtime_lease'](root, exclusive=True):
        if (root / 'Main.py').is_file():
            scope['snapshot'](root, label='설치 전 코드')
        _install_payload(root, sources=sources, preserve_config=preserve_config)
        modules = RUNTIME_FILES + ['Main.py', 'Scheduler.py', 'report_review.py']
        if not kept_config:
            modules.append('My_config.py')
        scope['_discard_runtime_bytecode'](root, modules)
        version = scope['snapshot'](root, label='설치된 코드')
    print('[setup] Code version:', version['id'])


def extract_sources(target_dir=None):
    """Opt-in development files only; never overwrite configuration or installed entry points."""
    root = Path(target_dir or Path(__file__).resolve().parent).resolve()
    root.mkdir(parents=True, exist_ok=True)
    bundle = read_bundle()
    runtime = root / 'auto_report_runtime.zip'
    if runtime.exists():
        with zipfile.ZipFile(runtime) as archive:
            for name in RUNTIME_FILES:
                bundle[name] = archive.read(name).decode('utf-8')
    for name in RUNTIME_FILES + ['gen_setup.py'] + sorted(n for n in bundle if n.startswith('tests/')):
        path = root / name
        if path.exists():
            print('[keep]', name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(bundle[name])
        print('[source]', name)


def rebuild(target_dir=None):
    root = Path(target_dir or Path(__file__).resolve().parent).resolve()
    bundle = read_bundle()
    path = root / 'gen_setup.py'
    source = path.read_text(encoding='utf-8') if path.exists() else bundle['gen_setup.py']
    scope = {'__name__': '_auto_report_builder', '__file__': str(path), '_EMBEDDED_BUILDER': source,
             '_EMBEDDED_BUNDLE': bundle}
    exec(compile(source, str(path), 'exec'), scope)
    scope['main']()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Auto Report compact installer / source tools')
    parser.add_argument('--target', help='Install/source/build directory (default: setup.py directory)')
    parser.add_argument('--preserve-config', action='store_true', help='Keep current My_config.py when installing code updates')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--extract-sources', action='store_true', help='Extract optional editable helper sources only')
    mode.add_argument('--build', action='store_true', help='Rebuild setup.py from installed files + runtime ZIP')
    mode.add_argument('--sources', action='store_true', help='Install with optional editable helper sources')
    args = parser.parse_args()
    if args.extract_sources:
        extract_sources(args.target)
    elif args.build:
        rebuild(args.target)
    else:
        install(args.target, sources=args.sources, preserve_config=args.preserve_config)
'''


def main():
    root = Path(__file__).resolve().parent
    bundle = {}
    for name in BUNDLE_FILES:
        path = root / name
        if path.is_file():
            bundle[name] = path.read_text(encoding='utf-8')
        elif name in RUNTIME_FILES and (root / 'auto_report_runtime.zip').is_file():
            with zipfile.ZipFile(root / 'auto_report_runtime.zip') as archive:
                bundle[name] = archive.read(name).decode('utf-8')
        elif name == 'gen_setup.py' and '_EMBEDDED_BUILDER' in globals():
            bundle[name] = _EMBEDDED_BUILDER
        elif name in globals().get('_EMBEDDED_BUNDLE', {}):
            bundle[name] = _EMBEDDED_BUNDLE[name]
        else:
            raise FileNotFoundError(path)
    for name, content in bundle.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
    compressed = gzip.compress(json.dumps(bundle, ensure_ascii=False).encode('utf-8'), mtime=0)
    checksum = hashlib.sha256(compressed).hexdigest()
    data = '\n'.join(textwrap.wrap(base64.b64encode(compressed).decode('ascii'), 76))
    header = '#!/usr/bin/env python3\n"""Auto Report compact release. Generated by gen_setup.py."""\n'
    content = (header + f'CHECKSUM = {checksum!r}\nDATA = """\n{data}\n"""\n'
               + f'RUNTIME_FILES = {RUNTIME_FILES!r}\nRETIRED_FILES = {RETIRED_FILES!r}\n' + INSTALLER)
    compile(content, 'setup.py', 'exec')
    output = root / 'setup.py'
    temp = root / '.setup.py.tmp'
    with temp.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
    temp.replace(output)
    print(f'[OK] {output}: {output.stat().st_size:,} bytes; {len(ENTRY_FILES)} extracted Python files')


if __name__ == '__main__':
    main()
