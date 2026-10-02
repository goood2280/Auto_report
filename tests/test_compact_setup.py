"""Release acceptance checks: fresh/upgrade installation, source editing and spawn, all offline."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = {'Main.py', 'Scheduler.py', 'My_config.py', 'report_review.py'}
MODULES = {'My_Function.py', 'anomaly_engine.py', 'operator_console.py', 'resource_governor.py', 'report_items.py', 'runtime_versions.py'}


def run(root, *args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env.pop('PYTHONPATH', None)
    env.pop('AUTO_REPORT_OPS_ROOT', None)
    result = subprocess.run([sys.executable, *args], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def installed_python_files(root):
    return {p.name for p in root.rglob('*.py') if '.runtime-versions' not in p.parts}


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    shutil.copy2(ROOT / 'setup.py', root / 'setup.py')
    run(root, 'setup.py')
    return root


def test_compact_install_import_paths_cli_and_spawn(installed, tmp_path):
    root = installed
    assert installed_python_files(root) == ENTRY | {'setup.py'}
    with zipfile.ZipFile(root / 'auto_report_runtime.zip') as archive:
        assert set(archive.namelist()) == MODULES
    assert (root / 'AGENTS.md').is_file()
    assert 'start_manager' not in (root / 'Scheduler.py').read_text(encoding='utf-8')
    assert 'self.manager' not in (root / 'My_config.py').read_text(encoding='utf-8')
    assert '--init-db' in run(root, 'Main.py', '--help')
    assert '--sync-wip' in run(root, 'Main.py', '--help')
    assert '--drain' in run(root, 'Scheduler.py', '--help')
    assert 'prepare' in run(root, 'report_review.py', '--help')
    script = root / 'spawn_check.py'
    script.write_text('''
import os
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import Main
import My_Function as mf
import anomaly_engine
import operator_console

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    assert '.zip' in mf.__file__
    assert Path(mf.operations_root()) == root / 'RUN' / 'OPS'
    before = mf.file_fingerprint([mf.__file__, anomaly_engine.__file__])
    assert before
    mf.ops_put('smoke', 'test', {'ok': True})
    assert mf.ops_get('smoke', 'test')['ok']
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn')) as pool:
        assert pool.submit(mf.operations_root).result(timeout=40) == str(root / 'RUN' / 'OPS')
        assert pool.submit(operator_console.plain, '\\x1b[91mOK\\x1b[0m').result(timeout=40) == 'OK'
    import zipfile
    with zipfile.ZipFile(root / 'auto_report_runtime.zip', 'a') as archive:
        archive.writestr('cache-version.txt', 'changed')
    assert mf.file_fingerprint([mf.__file__, anomaly_engine.__file__]) != before
''', encoding='utf-8')
    run(root, str(script))


def test_compact_wip_sync_cli_updates_final_without_et_query_or_reports(installed):
    output = run(installed, '-c', '''
import json, os, sys
from pathlib import Path
import pandas as pd
import Main as m
import My_Function as mf
root = Path.cwd()
assert '.zip' in mf.__file__
cfg = m.GLOBAL_CONFIG
cfg.settings = dict(vehicle='TEST', DB=str(root / 'DB') + os.sep,
    et_log_path=str(root / 'et.csv'), Final_et_log_path=str(root / 'et_Final.csv'),
    delay_min=15, unified_log=str(root / 'log.txt'), use_email_send=True,
    use_s3_upload=True, report_making=True, DB_Setting_mode=True)
cfg.load_from_yaml = lambda vehicle: None
pd.DataFrame([dict(prime_key='TEST_00001.01_CC100', lot_id='00001.01', dc_step_id='CC100',
    tkout_time='2020-01-01 09:00:00', dc_done=False)]).to_csv(root / 'et_Final.csv', index=False)
def query(params, **kwargs):
    assert params['table_name'] == 'fab.f_wip_current', 'ET query entered WIP-only command'
    return pd.DataFrame([dict(lot_id='00001.01', step_seq='CC200', last_update_date='2026-10-01 12:00:00')])
mf.getData_with_retry = query
os.environ['AUTO_REPORT_RESULT_PATH'] = str(root / 'wip-result.json')
sys.argv = ['Main.py', '_TRIGGER_WIP_SYNC_TEST']
assert m.main() == 0
assert pd.read_csv(root / 'et_Final.csv').dc_done.all()
assert mf.ops_get('db_setting_baseline', 'TEST')['source'] == 'wip_sync'
assert not mf.ops_list('reports') and not mf.ops_list('mail')
result = json.loads((root / 'wip-result.json').read_text(encoding='utf-8'))
assert result['status'] == 'success' and result['wip_sync']['completed'] == 1
assert not result['reports']
print('COMPACT_WIP_SYNC_OK')
''')
    assert 'COMPACT_WIP_SYNC_OK' in output


def test_upgrade_retires_owned_files_and_preserves_data(installed):
    root = installed
    old = {
        'Manager.py': 'old manager', 'manager_llm.py': 'old llm',
        'My_Function.py': 'old helper', 'gen_setup.py': 'old builder',
        'docs/MANAGER_START.md': 'old docs', 'My_config.py': '# operator settings',
    }
    for name, content in old.items():
        (root / name).write_text(content, encoding='utf-8')
    for name in ['RUN/DB/keep.parquet', 'RUN/QUEUE/scheduler_state.json',
                 'reformatter/config.yaml', '.env', 'custom.py']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('preserve', encoding='utf-8')
    run(root, 'setup.py')
    for name, content in old.items():
        backups = list((root / '.setup-backups').glob('*/' + name + '.bak'))
        assert len(backups) == 1
        assert backups[0].read_text(encoding='utf-8') == content
        if name != 'My_config.py':
            assert not (root / name).exists()
    for name in ['RUN/DB/keep.parquet', 'RUN/QUEUE/scheduler_state.json',
                 'reformatter/config.yaml', '.env', 'custom.py']:
        assert (root / name).read_text(encoding='utf-8') == 'preserve'
    run(root, 'setup.py')
    assert len(list((root / '.setup-backups').glob('*/My_config.py.bak'))) == 1


def test_rebuild_without_sources_and_with_edits(installed, tmp_path):
    root = installed
    original = (root / 'setup.py').read_bytes()
    run(root, 'setup.py', '--build')
    assert (root / 'setup.py').read_bytes() == original
    assert {p.name for p in root.glob('*.py')} == ENTRY | {'setup.py'}
    run(root, 'setup.py', '--extract-sources')
    assert (root / 'tests' / 'test_report_items.py').is_file()
    assert (root / 'tests' / 'test_report_review.py').is_file()
    source = root / 'operator_console.py'
    source.write_text(source.read_text(encoding='utf-8') + '\nEDITED = True\n', encoding='utf-8')
    config = (root / 'My_config.py').read_bytes()
    run(root, 'setup.py', '--extract-sources')
    assert 'EDITED = True' in source.read_text(encoding='utf-8')
    assert (root / 'My_config.py').read_bytes() == config
    assert 'True' in run(root, '-c', 'import Main, operator_console; print(operator_console.EDITED)')
    run(root, 'setup.py', '--build')
    target = tmp_path / 'edited-release'
    run(root, 'setup.py', '--target', str(target))
    with zipfile.ZipFile(target / 'auto_report_runtime.zip') as archive:
        assert b'EDITED = True' in archive.read('operator_console.py')
    assert installed_python_files(target) == ENTRY


def test_corrupt_bundle_fails_before_writes(tmp_path):
    spec = importlib.util.spec_from_file_location('release_setup', ROOT / 'setup.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    setup.CHECKSUM = 'invalid'
    with pytest.raises(ValueError, match='checksum'):
        setup.install(tmp_path / 'must-not-exist')
    assert not (tmp_path / 'must-not-exist').exists()


def test_code_upgrade_keeps_current_config_and_records_reversible_version(installed):
    import json
    import py_compile
    root = installed
    original = json.loads(run(root, 'report_review.py', 'current-version'))['id']
    (root / 'Main.py').write_text((root / 'Main.py').read_text(encoding='utf-8') + '\n# local code edit\n', encoding='utf-8')
    edited_main = (root / 'Main.py').read_bytes()
    config = root / 'My_config.py'
    config.write_text(config.read_text(encoding='utf-8') + '\nLOCAL_SETTING = 17\n', encoding='utf-8')
    config_bytes = config.read_bytes()
    modified = json.loads(run(root, 'report_review.py', 'save-version', '--label', 'local change'))['id']
    assert original != modified
    py_compile.compile(str(root / 'Main.py'), doraise=True)
    py_compile.compile(str(config), doraise=True)
    main_cache = Path(importlib.util.cache_from_source(str(root / 'Main.py')))
    config_cache = Path(importlib.util.cache_from_source(str(config)))
    config_bytecode = config_cache.read_bytes()
    assert main_cache.exists()
    run(root, 'setup.py', '--preserve-config')
    assert not main_cache.exists()
    assert config_cache.read_bytes() == config_bytecode
    assert config.read_bytes() == config_bytes
    assert json.loads(run(root, 'report_review.py', 'current-version'))['id'] == original
    run(root, 'report_review.py', 'rollback', '--version', modified)
    assert (root / 'Main.py').read_bytes() == edited_main
    assert config.read_bytes() == config_bytes
    assert json.loads(run(root, 'report_review.py', 'current-version'))['id'] == modified
