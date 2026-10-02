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
                    Final_et_log_path=str(tmp_path / 'et_Final.csv'),
                    QueryTimeSpan=3, SplitTimeSpan=2, now_minus=0, DB_Setting_mode=True,
                    et_force_full_refresh=True, db_setting_parallel=1)
    monkeypatch.setattr(mf.GLOBAL_CONFIG, 'settings', settings)
    return tmp_path, settings


def row(date, **changes):
    return dict(tkout_time=date, et_value=1., temperature=25, lot_id='L001_1',
                fab_lot_id='L001.1', step_id='S1', wafer_id=1, total_site_cnt=2,
                step_seq='P1', item_id='I', chip_x_pos=0, chip_y_pos=0, **changes)


@pytest.mark.parametrize('timestamp_values', [False, True], ids=['categorical-string', 'categorical-timestamp'])
def test_lot_log_uses_chronological_max_for_unordered_categorical_time(db, timestamp_values):
    root, settings = db
    times = ['2026-09-30 09:00:00', '2026-09-30 08:00:00']
    if timestamp_values:
        times = [pd.Timestamp(value) for value in times]
    frame = pd.DataFrame([row(times[0]), dict(row(times[1]), wafer_id=2)])
    frame['tkout_time'] = pd.Categorical(times, categories=times, ordered=False)
    frame['step_seq'] = pd.Categorical(['P1', 'P2'], categories=['P2', 'P1', 'UNUSED'], ordered=False)
    mf._merge_et_lot_log(frame, dict(settings, lock_wait_sec=0))
    log = pd.read_csv(root / 'et.csv')
    assert log.prime_key.tolist() == ['TEST_L001.1_S1']
    assert pd.Timestamp(log.tkout_time.iloc[0]) == pd.Timestamp('2026-09-30 09:00:00')
    assert log.wafer_id.iloc[0] == '[1, 2]'
    assert log.step_seq.iloc[0] == "['P1', 'P2']"


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
    final = pd.read_csv(root / 'et_Final.csv')
    assert final['dc_done'].all() and final.prime_key.tolist() == ['TEST_L001.1_S1']
    assert summary['history_completed'] == 1
    assert mf.ops_get('db_setting_baseline', 'TEST')['rows'] == 1


def test_query_failure_is_not_success_or_a_refresh_checkpoint(db, monkeypatch):
    monkeypatch.setattr(mf, 'getData_with_retry', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline failure')))
    with pytest.raises(RuntimeError, match='offline failure'):
        mf.etdata_query()
    assert mf.ops_get('et_refresh', 'TEST') is None
    assert mf.ops_get('db_setting_baseline', 'TEST') is None
    assert not (db[0] / 'et_Final.csv').exists()


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
    assert pd.read_csv(root / 'et_Final.csv').empty and result['history_completed'] == 0


def test_db_setup_merges_all_history_as_done_and_preserves_identifiers(db):
    root, settings = db
    settings['vehicle'] = 'PRODUCT_ALPHA'
    raw = pd.DataFrame([dict(prime_key='PRODUCT_ALPHA_00001.01_0100', wafer_id='[1, 2]',
                            step_seq="['P1']", total_site_cnt='[13]', tkout_time='2026-09-30 09:00:00')])
    raw.to_csv(root/'et.csv', index=False)
    prior = pd.DataFrame([dict(prime_key='PRODUCT_ALPHA_00001.01_0100', wafer_id='[1]',
                              step_seq="['P1']", total_site_cnt='[13]', tkout_time='2026-09-29 09:00:00',
                              lot_id='00001.01', dc_step_id='0100', dc_done=False),
                          dict(prime_key='PRODUCT_ALPHA_00002.01_0200', wafer_id='[3]',
                              step_seq="['P2']", total_site_cnt='[13]', tkout_time='2026-09-28 09:00:00',
                              lot_id='00002.01', dc_step_id='0200', dc_done=False)])
    prior.to_csv(root/'et_Final.csv', index=False)
    for _ in range(2):
        assert mf._initialize_et_completion_log(dict(settings, lock_wait_sec=0)) == 2
        final = pd.read_csv(root/'et_Final.csv', dtype={'lot_id':str, 'dc_step_id':str})
        assert final.dc_done.all() and not final.prime_key.duplicated().any()
        assert final.dc_step_id.tolist() == ['0200', '0100']
        assert final.lot_id.tolist() == ['00002.01', '00001.01']
        assert final.wafer_id.tolist() == ['[3]', '[1, 2]']
        assert mf.ops_get('db_setting_baseline', 'PRODUCT_ALPHA')['rows'] == 2
    assert not mf.ops_list('mail') and not mf.ops_list('reports')


@pytest.mark.parametrize('status,email', [('queued','pending'), ('failed','failed'), ('unknown','unknown'), ('success','disabled')])
def test_db_history_does_not_return_through_auto_retry_but_new_measurements_do(db, status, email):
    _, settings = db
    settings['use_email_send'] = True
    past = '2026-09-29 09:00:00'
    future = '2026-09-30 09:00:00'
    mf.ops_put('db_setting_baseline','TEST', dict(revisions={'TEST_L001.1_S1':past}))
    old = dict(id='old', vehicle='TEST', mode='AUTO', prime_key='TEST_L001.1_S1',
               tkout_time=past, status=status, email=email, attempts=0)
    mf.ops_put('reports','old', old)
    final = pd.DataFrame([dict(prime_key='TEST_L001.1_S1', lot_id='L001.1', dc_step_id='S1', dc_done=True, tkout_time=past),
                          dict(prime_key='TEST_L002.1_S1', lot_id='L002.1', dc_step_id='S1', dc_done=True, tkout_time=future)])
    candidates=final[['lot_id','dc_step_id','dc_done','tkout_time']].copy()
    result=main._retry_candidates(candidates,final,'TEST')
    assert result.lot_id.tolist() == ['L002.1']
    assert mf.ops_get('reports','old') == old  # No fabricated sent/success status.
    # An explicitly recorded retry of a later measurement of the same Lot/Step remains eligible.
    candidates=candidates.iloc[:1].copy(); candidates['tkout_time']=future
    assert main._exclude_db_setup_history(candidates,'TEST').lot_id.tolist() == ['L001.1']


def test_normal_et_refresh_does_not_mark_new_lots_done_or_move_baseline(db, monkeypatch):
    root, settings = db
    monkeypatch.setattr(mf, 'getData_with_retry', lambda *a, **k: pd.DataFrame([row(pd.Timestamp('2026-09-29 09:00:00'))]))
    mf.etdata_query()
    before=(root/'et_Final.csv').read_bytes()
    baseline=mf.ops_get('db_setting_baseline','TEST')
    settings.update(DB_Setting_mode=False, QueryTimeSpan=1)
    monkeypatch.setattr(mf, 'getData_with_retry', lambda *a, **k: pd.DataFrame([
        dict(row(pd.Timestamp('2026-09-30 09:00:00')), fab_lot_id='L002.1')]))
    summary=mf.etdata_query()
    assert 'history_completed' not in summary
    assert (root/'et_Final.csv').read_bytes() == before
    assert mf.ops_get('db_setting_baseline','TEST') == baseline
    assert set(pd.read_csv(root/'et.csv').prime_key) == {'TEST_L001.1_S1','TEST_L002.1_S1'}


def test_partial_query_failure_keeps_completion_baseline_unchanged(db, monkeypatch):
    root, settings = db
    settings['SplitTimeSpan']=1
    baseline=dict(revisions={'TEST_PREVIOUS_S1':'2026-09-27 09:00:00'})
    mf.ops_put('db_setting_baseline','TEST',baseline)
    prior=pd.DataFrame([dict(prime_key='TEST_PREVIOUS_S1', lot_id='PREVIOUS', dc_step_id='S1',
                            tkout_time='2026-09-27 09:00:00', dc_done=False)])
    prior.to_csv(root/'et_Final.csv',index=False);before=(root/'et_Final.csv').read_bytes()
    calls=[]
    def query(params, **kwargs):
        calls.append(params['dateFrom'])
        if len(calls)>1: raise RuntimeError('second chunk failed')
        return pd.DataFrame([row(pd.Timestamp(params['dateFrom']))])
    monkeypatch.setattr(mf,'getData_with_retry',query)
    with pytest.raises(RuntimeError,match='second chunk failed'): mf.etdata_query()
    assert len(calls)==2 and (root/'et_Final.csv').read_bytes()==before
    assert mf.ops_get('db_setting_baseline','TEST') == baseline
    assert mf.ops_get('et_refresh','TEST') is None


def test_failed_final_log_write_preserves_previous_baseline(db, monkeypatch):
    root, settings = db
    pd.DataFrame([dict(prime_key='TEST_L001.1_S1', tkout_time='2026-09-29 09:00:00')]).to_csv(root/'et.csv',index=False)
    baseline=dict(revisions={})
    mf.ops_put('db_setting_baseline','TEST',baseline)
    monkeypatch.setattr(pd.DataFrame,'to_csv',lambda *a,**k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError,match='disk full'): mf._initialize_et_completion_log(dict(settings,lock_wait_sec=0))
    assert mf.ops_get('db_setting_baseline','TEST') == baseline
    assert not (root/'et_Final.csv').exists()


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


@pytest.fixture
def wip_sync(db):
    root, settings = db
    settings.update(vehicle='PRODUCT_ALPHA', delay_min=15, lock_wait_sec=0)
    lots = ['L001.1', 'L002.1', 'L003.1', 'L004.1', 'L005.1', 'L006.1', 'L007.1', '00008.01']
    rows = [dict(prime_key=f'PRODUCT_ALPHA_{lot}_{"0010" if i == 7 else "CC100"}',
                 wafer_id='[1, 2]', step_seq="['P1']", total_site_cnt='[13]',
                 tkout_time='2026-09-30 11:55:00' if i == 3 else '2026-09-29 09:00:00',
                 lot_id=lot, dc_step_id='0010' if i == 7 else 'CC100', dc_done='True' if i == 5 else 'False')
            for i, lot in enumerate(lots)]
    prior = pd.DataFrame(rows)
    prior.to_csv(settings['Final_et_log_path'], index=False)
    # Include final-only history: its fresh WIP must be checked as well.
    prior[~prior.lot_id.eq('L007.1')].drop(columns=['lot_id', 'dc_step_id', 'dc_done']).to_csv(settings['et_log_path'], index=False)
    steps = ['CC200', 'CC100', 'CC150', 'CC200', None, 'CC100', 'CC100', '0010']
    wip = pd.DataFrame([dict(lot_id=lot, step_id=step, last_update_date='2026-09-30 10:00:00')
                        for lot, step in zip(lots, steps) if step is not None]
                       + [dict(lot_id='L001.1', step_id='CC100', last_update_date='2026-09-30 08:00:00')])
    return root, settings, wip


@pytest.mark.parametrize('args', [['--sync-wip', 'vehicle_A'], ['_TRIGGER_WIP_SYNC_vehicle_A'], ['TRIGGER_WIP_SYNC_vehicle_A']])
def test_wip_sync_parser_and_delivery_disabled(args):
    command = main._parse_command(args)
    assert command == dict(argument='vehicle_A', kind='sync_wip', recipient=None)
    assert main._parse_trigger('_TRIGGER_WIP_SYNC_vehicle_A') == ('WIP_SYNC', 'vehicle_A', None, None, None)
    cfg = SimpleNamespace(settings=dict(DB_Setting_mode=True, test_mode=True, report_making=True,
                                        use_email_send=True, use_s3_upload=True))
    main._apply_command_settings(command, cfg)
    assert not any(cfg.settings.values())


@pytest.mark.parametrize('args', [
    ['--sync-wip', '../TEST'], ['--sync-wip', 'TRIGGER_X'], ['_TRIGGER_WIP_SYNC_'],
    ['--sync-wip', 'TEST', '--days', '30'], ['_TRIGGER_WIP_SYNC_TEST', '--parallel', '2'],
    ['--sync-wip', 'TEST', '--prime-key', 'TEST_L_S'], ['--sync-wip', 'TEST', '--single'],
    ['--sync-wip', 'TEST', '--init-db', 'TEST'], ['--send-user', 'user', '_TRIGGER_WIP_SYNC_TEST'],
])
def test_invalid_wip_sync_options_are_rejected(args):
    with pytest.raises(SystemExit):
        main._parse_command(args)


def test_wip_sync_checks_all_history_preserves_flags_and_pending(wip_sync):
    root, settings, wip = wip_sync
    raw_before = Path(settings['et_log_path']).read_bytes()
    mf.ops_put('db_setting_baseline', settings['vehicle'], dict(revisions={'PRODUCT_ALPHA_PREVIOUS_CC100':'2026-09-28 09:00:00'}))
    for _ in range(2):
        result = mf.sync_wip_et_completion(settings, wip)
        final = pd.read_csv(settings['Final_et_log_path'], dtype={'lot_id':str, 'dc_step_id':str})
        flags = dict(zip(final.lot_id, final.dc_done))
        assert flags == {'L001.1':True, 'L002.1':False, 'L003.1':False, 'L004.1':False,
                         'L005.1':True, 'L006.1':True, 'L007.1':False, '00008.01':False}
        assert result['rows'] == 8 and result['completed'] == 3 and result['pending'] == 5
        assert final.loc[final.lot_id.eq('00008.01'), 'dc_step_id'].item() == '0010'
        assert final.wafer_id.eq('[1, 2]').all() and not final.prime_key.duplicated().any()
        assert Path(settings['et_log_path']).read_bytes() == raw_before
        baseline = mf.ops_get('db_setting_baseline', settings['vehicle'])
        assert len(baseline['revisions']) == 4 and baseline['source'] == 'wip_sync'
        assert 'PRODUCT_ALPHA_PREVIOUS_CC100' in baseline['revisions']
    assert result['newly_completed'] == 0
    assert not mf.ops_list('reports') and not mf.ops_list('mail')


@pytest.mark.parametrize('status', ['queued', 'running', 'failed', 'unknown', 'success'])
def test_wip_sync_blocks_old_retry_and_allows_future_completion(wip_sync, status):
    _, settings, wip = wip_sync
    record = dict(id='old', vehicle=settings['vehicle'], mode='AUTO', prime_key='PRODUCT_ALPHA_L001.1_CC100',
                  tkout_time='2026-09-29 09:00:00', status=status, email='disabled', attempts=0,
                  saved=True, paths={'html':'old.html', 'ppt':'old.pptx'})
    settings['use_email_send'] = True
    mf.ops_put('reports', 'old', record)
    mf.sync_wip_et_completion(settings, wip)
    final = pd.read_csv(settings['Final_et_log_path'])
    candidates = final.loc[final.dc_done, ['lot_id', 'dc_step_id', 'dc_done', 'tkout_time']]
    assert main._retry_candidates(candidates, final, settings['vehicle']).empty
    assert mf.ops_get('reports', 'old') == record
    # A still-pending measurement becomes completed in the next regular pass, not baselined away.
    wip.loc[wip.lot_id.eq('L002.1'), 'step_id'] = 'CC200'
    next_final = mf._et_completion_frame(final, pd.read_csv(settings['et_log_path']), wip,
                                          settings['vehicle'], 15, datetime(2026, 9, 30, 12))
    fresh = next_final.loc[next_final.dc_done & ~next_final.prime_key.isin(final.loc[final.dc_done, 'prime_key']),
                           ['lot_id', 'dc_step_id', 'dc_done', 'tkout_time']]
    assert main._retry_candidates(fresh, next_final, settings['vehicle']).lot_id.tolist() == ['L002.1']


@pytest.mark.parametrize('fault', ['columns', 'lot', 'step', 'time', 'timestamp', 'product', 'flag', 'write'])
def test_wip_sync_bad_data_or_save_failure_preserves_final_and_baseline(wip_sync, monkeypatch, fault):
    _, settings, wip = wip_sync
    if fault == 'columns':wip = wip.drop(columns='last_update_date')
    elif fault in ('lot', 'step', 'time'):
        wip.loc[0, {'lot':'lot_id', 'step':'step_id', 'time':'last_update_date'}[fault]] = None
    elif fault == 'timestamp':wip.loc[0, 'last_update_date'] = 'invalid'
    elif fault in ('product', 'flag'):
        prior = pd.read_csv(settings['Final_et_log_path'])
        if fault == 'product':prior.loc[0, 'prime_key'] = 'WRONG_L001.1_CC100'
        else:prior['dc_done'] = prior.dc_done.astype(str); prior.loc[0, 'dc_done'] = 'invalid'
        prior.to_csv(settings['Final_et_log_path'], index=False)
    elif fault == 'write':
        def fail(*args):raise OSError('disk full')
        monkeypatch.setattr(mf, 'atomic_output', fail)
    original = Path(settings['Final_et_log_path']).read_bytes()
    baseline = dict(revisions={'previous':'2026-09-28 09:00:00'})
    mf.ops_put('db_setting_baseline', settings['vehicle'], baseline)
    with pytest.raises((ValueError, OSError)):
        mf.sync_wip_et_completion(settings, wip)
    assert Path(settings['Final_et_log_path']).read_bytes() == original
    assert mf.ops_get('db_setting_baseline', settings['vehicle']) == baseline


def test_wip_sync_query_failure_does_not_use_cached_wip(wip_sync, monkeypatch):
    root, settings, wip = wip_sync
    settings['DB'] = str(root) + os.sep
    cache = root / f'{settings["vehicle"]}_wip_current.csv'
    wip.to_csv(cache, index=False)
    cache_before = cache.read_bytes()
    final_before = Path(settings['Final_et_log_path']).read_bytes()
    def fail(*args, **kwargs):raise RuntimeError('offline query failed')
    monkeypatch.setattr(mf, 'getData_with_retry', fail)
    monkeypatch.setattr(main.GLOBAL_CONFIG, 'load_from_yaml', lambda name: None)
    monkeypatch.setattr(main, '_RUN', SimpleNamespace(data={}, stage=lambda name: None))
    monkeypatch.setattr(main.builtins, 'print', main._original_print)
    monkeypatch.setattr(main, '_LOG_PATH', None)
    with pytest.raises(RuntimeError, match='offline query failed'):
        main._main_impl(main._parse_command(['--sync-wip', settings['vehicle']]))
    assert cache.read_bytes() == cache_before and Path(settings['Final_et_log_path']).read_bytes() == final_before
    assert mf.ops_get('db_setting_baseline', settings['vehicle']) is None


def test_wip_sync_main_only_queries_wip_without_analysis_or_delivery(wip_sync, monkeypatch):
    root, settings, wip = wip_sync
    settings.update(DB=str(root) + os.sep, use_email_send=True, use_s3_upload=True, test_mode=True,
                    report_making=True, DB_Setting_mode=True)
    calls = []
    def query(params, **kwargs):
        calls.append(params)
        return wip.rename(columns={'step_id':'step_seq'}).copy()
    monkeypatch.setattr(mf, 'getData_with_retry', query)
    monkeypatch.setattr(main.GLOBAL_CONFIG, 'load_from_yaml', lambda name: None)
    monkeypatch.setattr(main, '_RUN', SimpleNamespace(data={}, stage=lambda name: None))
    monkeypatch.setattr(main.builtins, 'print', main._original_print)
    monkeypatch.setattr(main, '_LOG_PATH', None)
    for name in ('etdata_query', 'reformatter_verify', '_send_report_files', '_queue_auto_reports'):
        monkeypatch.setattr(main, name, lambda *a, **k: pytest.fail('WIP sync entered report/ET path'))
    main._main_impl(main._parse_command(['_TRIGGER_WIP_SYNC_' + settings['vehicle']]))
    assert [call['table_name'] for call in calls] == ['fab.f_wip_current']
    assert main._RUN.data['wip_sync']['completed'] == 3 and main._RUN.data['vehicle'] == settings['vehicle']
    assert not mf.ops_list('reports') and not mf.ops_list('mail')


def test_wip_sync_keeps_executor_and_product_locks(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    monkeypatch.setattr(sys, 'argv', ['Main.py', '--sync-wip', 'TEST'])
    monkeypatch.setattr(main, 'OperationRun', lambda arg: SimpleNamespace(stage=lambda name: events.append(name), finish=lambda *args: 0))
    def serial(fn):
        events.append('executor_locked'); result = fn(); events.append('executor_released'); return result
    @contextmanager
    def product(path, **kwargs):
        assert Path(path).name == 'TEST.lock'
        events.append('product_locked'); yield; events.append('product_released')
    monkeypatch.setattr(main, '_execute_serially', serial)
    monkeypatch.setattr(main, 'process_lock', product)
    monkeypatch.setattr(main, '_main_impl', lambda command: events.append(command['kind']))
    monkeypatch.setattr(main, '_drain_uploads', lambda **kwargs: None)
    assert main.main() == 0
    assert events == ['waiting_execution', 'executor_locked', 'product_locked', 'startup', 'sync_wip', 'product_released', 'executor_released']


@pytest.mark.parametrize('step,expected', [('CC100',False), ('CC199',False), ('CC200',True), ('DD100',True), (None,True)])
def test_wip_completion_uses_original_advance_rule_and_delay(step, expected):
    raw = pd.DataFrame([dict(prime_key='TEST_00001.01_CC100', tkout_time='2026-09-30 09:00:00')])
    wip = pd.DataFrame(columns=['lot_id','step_id','last_update_date']) if step is None else pd.DataFrame([
        dict(lot_id='00001.01', step_id=step, last_update_date='2026-09-30 10:00:00')])
    final = mf._et_completion_frame(pd.DataFrame(), raw, wip, 'TEST', 15, datetime(2026, 9, 30, 12))
    assert bool(final.dc_done.item()) is expected
    # Exactly the delay boundary still waits, matching the normal report condition.
    final = mf._et_completion_frame(pd.DataFrame(), raw, wip, 'TEST', 15, datetime(2026, 9, 30, 9, 15))
    assert not final.dc_done.item()


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
    frame = pd.DataFrame(rows)
    # Corporate query responses can dictionary-encode timestamps as unordered categories.
    frame['tkout_time'] = pd.Categorical(frame['tkout_time'], ordered=False)
    frame['step_seq'] = pd.Categorical(frame['step_seq'], ordered=False)
    return frame
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
        et_log_path=str(root / 'et.csv'), Final_et_log_path=str(root / 'et_Final.csv'), QueryTimeSpan=7, SplitTimeSpan=2, now_minus=0,
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
        final = pd.read_csv(root / 'et_Final.csv')
        assert final.prime_key.tolist() == ['TEST_L001.1_S1'] and final.dc_done.all()
        assert summary['history_completed'] == 1
        files = list((root / 'daily').glob('date=*/data.parquet'))
        assert len(files) == 7
        assert all(len(pd.read_parquet(path)) == 1 for path in files)
        assert all(pd.api.types.is_datetime64_any_dtype(pd.read_parquet(path)['tkout_time']) for path in files)
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
