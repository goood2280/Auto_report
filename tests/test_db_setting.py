"""DB setting acceptance with synthetic data, isolated storage and offline APIs."""
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import Main as main
import My_Function as mf
import Scheduler as scheduler
import resource_governor as governor


@pytest.mark.parametrize('prefix', ['_TRIGGER_DB_SETTING_', 'TRIGGER_DB_SETTING_'])
def test_db_setting_trigger_accepts_product_with_underscores(prefix):
    command = main._parse_command([prefix + 'vehicle_A', '--days', '30', '--parallel', '4'])
    assert command == dict(argument='vehicle_A', kind='init_db', recipient=None, days=30, parallel=4)
    assert main._parse_trigger(prefix + 'vehicle_A') == ('DB_SETTING', 'vehicle_A', None, None, None)


@pytest.mark.parametrize('args', [
    ['--init-db', 'TEST', '--days', '0'], ['--init-db', 'TEST', '--parallel', '-1'],
    ['--init-db', 'TEST', '--days', '1.5'], ['--init-db', '../TEST'],
    ['_TRIGGER_DB_SETTING_', '--days', '30'], ['TEST', '--parallel', '2'],
    ['_TRIGGER_TEST_L1_S1', '--days', '30'],
    ['_TRIGGER_DB_SETTING_TEST', '--send-user', 'user', '--prime-key', 'TEST_L1_S1'],
])
def test_invalid_db_options_fail_before_processing(args):
    with pytest.raises(SystemExit):
        main._parse_command(args)


def test_yaml_db_defaults_and_cli_override(monkeypatch):
    cfg = main.GLOBAL_CONFIG
    monkeypatch.setattr(cfg, 'settings', dict(db_setting_days=45, db_setting_parallel=3))
    main._apply_command_settings(main._parse_command(['--init-db', 'TEST']), cfg)
    assert cfg.get('QueryTimeSpan') == 45 and cfg.get('db_setting_parallel') == 3
    main._apply_command_settings(main._parse_command(['--init-db', 'TEST', '--days', '1', '--parallel', '2']), cfg)
    assert cfg.get('QueryTimeSpan') == 1 and cfg.get('db_setting_parallel') == 2
    assert cfg.get('et_force_full_refresh') and cfg.get('now_minus') == 0
    assert not any(cfg.get(key) for key in ('report_making', 'test_mode', 'use_email_send', 'use_s3_upload'))


@pytest.mark.parametrize('db_request', [dict(kind='init_db'), dict(mode='DB_SETTING')])
def test_scheduler_preserves_db_days_and_parallel(db_request):
    req, why = scheduler._norm_request(dict(vehicle='TEST', days=30, parallel=4, **db_request), 'test')
    assert not why and req['kind'] == 'init_db' and req['generate_only']
    assert scheduler.main_arguments(req, []) == ['--init-db', 'TEST', '--days', '30', '--parallel', '4']


@pytest.mark.parametrize('field,value', [('days', 0), ('days', True), ('parallel', 2.5), ('parallel', '4')])
def test_scheduler_rejects_invalid_db_options(field, value):
    req, why = scheduler._norm_request(dict(kind='init_db', vehicle='TEST', **{field: value}), 'test')
    assert req is None and field in why


def test_report_queue_cannot_silently_ignore_db_options():
    req, why = scheduler._norm_request(dict(vehicle='TEST', lot_id='L1', step_id='S1', days=30), 'test')
    assert req is None and 'DB setting' in why


def test_db_setting_skips_executor_and_keeps_product_lock(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    monkeypatch.setattr(sys, 'argv', ['Main.py', '_TRIGGER_DB_SETTING_TEST', '--days', '1'])
    monkeypatch.setattr(main, 'OperationRun', lambda arg: SimpleNamespace(
        stage=lambda name: events.append(name), finish=lambda *args: 0))
    monkeypatch.setattr(main, '_execute_serially', lambda fn: pytest.fail('DB setting waited on executor'))
    monkeypatch.setattr(main, '_main_impl', lambda command: events.append(command['kind']))
    monkeypatch.setattr(main, '_drain_uploads', lambda **kwargs: None)

    @contextmanager
    def lock(path, **kwargs):
        assert Path(path).name == 'TEST.lock'
        events.append('product_locked')
        yield
        events.append('product_released')
    monkeypatch.setattr(main, 'process_lock', lock)
    assert main.main() == 0
    assert events == ['waiting_product', 'product_locked', 'startup', 'init_db', 'product_released']


def test_db_entry_completes_while_another_process_holds_executor(tmp_path):
    ops = tmp_path / 'ops'
    env = dict(os.environ, AUTO_REPORT_OPS_ROOT=str(ops), AUTO_REPORT_EXECUTION_WAIT_SEC='0',
               PYTHONIOENCODING='utf-8')
    script = '''
import sys
import Main as m
m._main_impl = lambda command: None
m._drain_uploads = lambda **kwargs: None
sys.argv = ['Main.py', '_TRIGGER_DB_SETTING_TEST', '--days', '1']
assert m.main() == 0
print('EXECUTOR_BYPASSED')
'''
    with mf.process_lock(str(ops / 'locks/executor.lock')):
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EXECUTOR_BYPASSED' in result.stdout


def test_db_only_pipeline_does_not_query_wip_or_select_reports(monkeypatch):
    cfg = main.GLOBAL_CONFIG
    monkeypatch.setattr(cfg, 'settings', dict(vehicle='TEST'))
    monkeypatch.setattr(cfg, 'load_from_yaml', lambda name: None)
    monkeypatch.setattr(main, '_RUN', SimpleNamespace(data={}, stage=lambda name: None))
    monkeypatch.setattr(main, 'etdata_query', lambda: {'partitions': 1})
    monkeypatch.setattr(main, 'wipdata_query', lambda: pytest.fail('DB-only mode queried WIP'))
    monkeypatch.setattr(main, 'print_status', lambda *args: None)
    # Preserve process-wide print hook after the entry point installs its logger.
    monkeypatch.setattr(main.builtins, 'print', main._original_print)
    monkeypatch.setattr(main, '_LOG_PATH', None)
    main._main_impl(main._parse_command(['--init-db', 'TEST', '--days', '1']))
    assert main._RUN.data['db_setting'] == {'partitions': 1}


@pytest.fixture
def db(tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 12)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    monkeypatch.setenv('AUTO_REPORT_SLOT_DIR', str(tmp_path / 'slots'))
    monkeypatch.setattr(mf, 'datetime', FrozenDatetime)
    (tmp_path / 'reformatter').mkdir()
    pd.DataFrame([dict(CATEGORY='REAL', ITEMID='I')]).to_csv('reformatter/TEST_reformatter.csv', index=False)
    settings = dict(vehicle='TEST', DB_et_daily=str(tmp_path / 'daily'), et_log_path=str(tmp_path / 'et.csv'),
                    QueryTimeSpan=3, SplitTimeSpan=2, now_minus=0, DB_Setting_mode=True,
                    et_force_full_refresh=True, db_setting_parallel=1)
    monkeypatch.setattr(mf.GLOBAL_CONFIG, 'settings', settings)
    return tmp_path, settings


def row(date, **changes):
    return dict(tkout_time=date, et_value=1., temperature=25, lot_id='L001_1',
                fab_lot_id='L001.1', step_id='S1', wafer_id=1, total_site_cnt=2,
                step_seq='P1', item_id='I', chip_x_pos=0, chip_y_pos=0, **changes)


def test_reloading_overlap_replaces_days_and_removes_only_duplicate_rows(db, monkeypatch):
    root, settings = db
    calls = []
    value = [1.]
    def query(params, **kwargs):
        calls.append((params['dateFrom'], params['dateTo']))
        rows = []
        for date in pd.date_range(params['dateFrom'], params['dateTo']):
            first = row(date)
            first['et_value'] = value[0]
            second = dict(first, chip_x_pos=1)
            rows.extend([first, dict(first), second])
        # An overlapping source boundary is ignored rather than replacing a different day's snapshot.
        rows.append(row(pd.Timestamp(params['dateFrom']) - pd.Timedelta(days=1)))
        return pd.DataFrame(rows)
    monkeypatch.setattr(mf, 'getData_with_retry', query)
    old = root / 'daily/date=2026-09-01/data.parquet'
    old.parent.mkdir(parents=True)
    pd.DataFrame([row(pd.Timestamp('2026-09-01'))]).to_parquet(old, index=False)
    before = old.read_bytes()
    for amount in (1., 9.):
        value[0] = amount
        summary = mf.etdata_query()
        assert summary['rows'] == 6 and summary['duplicates'] == 3 and summary['partitions'] == 3
        for day in ('2026-09-28', '2026-09-29', '2026-09-30'):
            frame = pd.read_parquet(root / f'daily/date={day}/data.parquet')
            assert len(frame) == 2 and not frame.duplicated().any()
            assert frame['et_value'].tolist() == [amount, amount]
    assert calls == [('2026-09-28', '2026-09-29'), ('2026-09-30', '2026-09-30')] * 2
    assert len(list((root / 'daily').glob('date=*/data.parquet'))) == 4
    assert old.read_bytes() == before
    assert pd.read_csv(root / 'et.csv')['prime_key'].tolist() == ['TEST_L001.1_S1']


def test_query_failure_is_not_success_or_a_refresh_checkpoint(db, monkeypatch):
    monkeypatch.setattr(mf, 'getData_with_retry', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline failure')))
    with pytest.raises(RuntimeError, match='offline failure'):
        mf.etdata_query()
    assert mf.ops_get('et_refresh', 'TEST') is None


def test_failed_parquet_replacement_keeps_previous_day(db, monkeypatch):
    root, settings = db
    settings['QueryTimeSpan'] = 1
    path = root / 'daily/date=2026-09-30/data.parquet'
    path.parent.mkdir(parents=True)
    pd.DataFrame([row(pd.Timestamp('2026-09-30'))]).to_parquet(path, index=False)
    original = path.read_bytes()
    monkeypatch.setattr(mf, 'getData_with_retry', lambda *a, **k: pd.DataFrame([row(pd.Timestamp('2026-09-30'))]))
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        mf.etdata_query()
    assert path.read_bytes() == original
    assert mf.ops_get('et_refresh', 'TEST') is None
    assert not list(path.parent.glob('._writing_*'))


def test_one_day_and_empty_query_are_valid(db, monkeypatch):
    root, settings = db
    settings['QueryTimeSpan'] = 1
    calls = []
    monkeypatch.setattr(mf, 'getData_with_retry', lambda params, **kwargs: calls.append(params) or pd.DataFrame())
    result = mf.etdata_query()
    assert result['rows'] == 0 and result['chunks'] == 1
    assert calls[0]['dateFrom'] == calls[0]['dateTo'] == '2026-09-30'
    assert pd.read_csv(root / 'et.csv').empty


def test_parallel_failure_waits_for_workers_and_releases_slots(db, monkeypatch):
    from concurrent.futures import Future
    import concurrent.futures
    _, settings = db
    settings.update(db_setting_parallel=2, SplitTimeSpan=1)
    events = []
    monkeypatch.setattr(governor, 'plan_workers', lambda *a, **k: dict(workers=2, reason='test'))
    monkeypatch.setattr(governor, 'release_all', lambda: events.append('slots_released'))
    class Pool:
        def __init__(self, **kwargs):
            assert kwargs['mp_context'].get_start_method() == 'spawn'
        def submit(self, fn, task):
            events.append('submitted')
            future = Future()
            future.set_exception(RuntimeError('offline pool failure'))
            return future
        def shutdown(self, **kwargs):
            assert kwargs == dict(wait=True, cancel_futures=True)
            events.append('workers_finished')
    monkeypatch.setattr(concurrent.futures, 'ProcessPoolExecutor', Pool)
    with pytest.raises(RuntimeError, match='offline pool failure'):
        mf.etdata_query()
    assert events == ['submitted', 'submitted', 'workers_finished', 'slots_released']
    assert mf.ops_get('et_refresh', 'TEST') is None


@pytest.mark.parametrize('compact', [False, True], ids=['source', 'compact-install'])
def test_real_spawn_queries_merge_logs_and_replace_repeated_days(tmp_path, compact):
    root = tmp_path / 'installed'
    root.mkdir()
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(ROOT),
               AUTO_REPORT_OPS_ROOT=str(root / 'ops'), AUTO_REPORT_SLOT_DIR=str(root / 'slots'))
    if compact:
        shutil.copy2(ROOT / 'setup.py', root / 'setup.py')
        installed = subprocess.run([sys.executable, 'setup.py'], cwd=root, env=env,
                                   capture_output=True, text=True, encoding='utf-8', timeout=60)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        env.pop('PYTHONPATH')
    (root / 'reformatter').mkdir()
    pd.DataFrame([dict(CATEGORY='REAL', ITEMID='I')]).to_csv(root / 'reformatter/TEST_reformatter.csv', index=False)
    (root / 'bigdataquery.py').write_text('''
import json, os, time
from pathlib import Path
import pandas as pd
def getData(params, **kwargs):
    started = time.monotonic()
    time.sleep(1)
    rows = [dict(tkout_time=date, et_value=1., temperature=25, lot_id='L001_1', fab_lot_id='L001.1',
                 step_id='S1', wafer_id=date.day, total_site_cnt=1, step_seq='P1', item_id='I')
            for date in pd.date_range(params['dateFrom'], params['dateTo'])]
    rows.append(dict(rows[0]))
    events = Path('query_events'); events.mkdir(exist_ok=True)
    (events / (params['dateFrom'] + '.json')).write_text(json.dumps(
        dict(pid=os.getpid(), started=started, ended=time.monotonic())))
    return pd.DataFrame(rows)
''', encoding='utf-8')
    (root / 'check_db.py').write_text('''
import ast, json
from datetime import datetime
from pathlib import Path
import pandas as pd
import Main
import My_Function as mf
import resource_governor as governor
class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):return cls(2026, 9, 30, 12)
if __name__ == '__main__':
    root = Path.cwd()
    if (root / 'auto_report_runtime.zip').exists():assert '.zip' in mf.__file__
    mf.datetime = FrozenDatetime
    mf.GLOBAL_CONFIG.settings = dict(vehicle='TEST', DB_et_daily=str(root / 'daily'),
        et_log_path=str(root / 'et.csv'), QueryTimeSpan=7, SplitTimeSpan=2, now_minus=0,
        DB_Setting_mode=True, et_force_full_refresh=True, db_setting_parallel=3)
    governor.usable_cores = lambda: 4
    governor.busy_cores = lambda cores, sample_sec=0.25: 0.
    governor.available_memory_gb = lambda: 64.
    for _ in range(2):
        summary = mf.etdata_query()
        assert summary['workers'] == 3 and summary['rows'] == 7 and summary['duplicates'] == 4
        assert governor.held_slots() == 0
        log = pd.read_csv(root / 'et.csv')
        assert log.prime_key.tolist() == ['TEST_L001.1_S1']
        assert ast.literal_eval(log.wafer_id.iloc[0]) == list(range(24, 31))
        files = list((root / 'daily').glob('date=*/data.parquet'))
        assert len(files) == 7
        assert all(len(pd.read_parquet(path)) == 1 for path in files)
    events = [json.loads(path.read_text()) for path in (root / 'query_events').glob('*.json')]
    assert len({event['pid'] for event in events}) >= 2
    assert any(a['started'] < b['ended'] and b['started'] < a['ended']
               for a in events for b in events if a['pid'] != b['pid'])
    print('OFFLINE_PARALLEL_DB_OK')
''', encoding='utf-8')
    result = subprocess.run([sys.executable, 'check_db.py'], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OFFLINE_PARALLEL_DB_OK' in result.stdout
