"""Multi-lot triggers, server-wide worker budget and async upload bookkeeping (offline)."""
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import Main as main
import My_Function as mf
import Scheduler as scheduler
import resource_governor as governor


@pytest.mark.parametrize('lot,step,expected', [
    ('L1', 'S1', [('L1', 'S1')]),
    ('L1,L2,L3', 'S1', [('L1', 'S1'), ('L2', 'S1'), ('L3', 'S1')]),
    ('L1', 'S1,S2', [('L1', 'S1'), ('L1', 'S2')]),
    ('L1,L2', 'S1,S2', [('L1', 'S1'), ('L2', 'S2')]),
    ('L1,L1', 'S1,S1', [('L1', 'S1')]),
])
def test_trigger_pairs_rules_match_in_main_and_scheduler(lot, step, expected):
    assert mf.trigger_pairs(lot, step) == expected
    assert scheduler._pairs(lot, step) == expected


@pytest.mark.parametrize('lot,step', [('L1,L2,L3', 'S1,S2'), ('L_1', 'S1'), ('', 'S1')])
def test_trigger_pairs_reject_ambiguous_or_invalid(lot, step):
    with pytest.raises(ValueError):
        mf.trigger_pairs(lot, step)
    with pytest.raises(ValueError):
        scheduler._pairs(lot, step)


def test_multi_lot_trigger_command_parses_and_keeps_lists(tmp_path, monkeypatch):
    assert main._parse_trigger('_TRIGGER_SINGLE_vehicle_A_L1.1,L2.1_S1') == ('SINGLE', 'vehicle_A', 'L1.1,L2.1', 'S1', None)
    command = main._parse_command(['--send-user', 'hong', '--prime-key', 'vehicle_A_L1.1,L2.1_S1,S2', '--single'])
    assert command['argument'] == '_TRIGGER_SINGLE_vehicle_A_L1.1,L2.1_S1,S2'


def _daily(tmp_path):
    rows = []
    for lot, step, day in [('L1.1', 'S1', '2025-01-01'), ('L2.1', 'S1', '2025-01-02'), ('L3.1', 'S2', '2025-01-03')]:
        rows.append(dict(fab_lot_id=lot, lot_id=lot, root_lot_id=lot[:2], wafer_id=1, step_id=step, step_seq='P',
                         tkout_time=pd.Timestamp(day), item_id='I1', et_value=1.0, date=day))
    frame = pd.DataFrame(rows)
    for day, part in frame.groupby('date'):
        folder = tmp_path / 'daily' / f'date={day}'
        folder.mkdir(parents=True)
        part.drop(columns='date').to_parquet(folder / 'data.parquet', index=False)
    return str(tmp_path / 'daily')


def test_single_multi_loads_only_listed_pairs_without_period(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    formatter = pd.DataFrame([dict(CATEGORY='REAL', ITEMID='I1', ALIAS='A1')])
    database = _daily(tmp_path)
    with duckdb.connect() as conn:
        both = mf.load_daily_projected(conn, database, None, formatter, lot='L1.1,L3.1', step='S1,S2')
        cached = mf.load_daily_projected(conn, database, None, formatter, lot='L1.1,L3.1', step='S1,S2')
        injected = mf.load_daily_projected(conn, database, None, formatter, lot="L1.1' OR 1=1 --,L2.1", step='S1')
    assert sorted(both.fab_lot_id) == ['L1.1', 'L3.1']
    assert cached.equals(both)
    assert list(injected.fab_lot_id) == ['L2.1']


def test_force_period_uses_oldest_target_and_skips_missing():
    log = pd.DataFrame([
        {'lot_id': 'A.1', 'dc_step_id': 'S1', 'tkout_time': '2026-09-01 08:00:00'},
        {'lot_id': 'B.1', 'dc_step_id': 'S1', 'tkout_time': '2026-09-20 08:00:00'},
    ])
    now = pd.Timestamp('2026-09-28').to_pydatetime()
    assert main._force_viewing_period_all(log, 'A.1,B.1,C.1', 'S1', 7, now) == 29
    with pytest.raises(ValueError):
        main._force_viewing_period_all(log, 'C.1', 'S1', 7, now)


def test_scheduler_drops_already_published_pairs_and_records_each(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'known_vehicles', lambda: {'V'})
    monkeypatch.setattr(scheduler, 'log', lambda *a, **k: None)
    state = scheduler.State(str(tmp_path / 'state.json'))
    state.data['done_targets'] = {'V|L1|S1': 'yesterday'}
    cfg = {'trigger': {'enabled': True, 'email_receiver': ['G'],
                       'allowed_email_receiver': ['G'], 'history_keep': 20},
           'watchdog': {'ops_root': str(tmp_path / 'ops')}}
    request, why = scheduler._norm_request({'req_id': 'm1', 'vehicle': 'V', 'lot_id': 'L1,L2,L3', 'step_id': 'S1'}, 't')
    assert why == '' and (request['lot_id'], request['step_id']) == ('L1,L2,L3', 'S1,S1,S1')
    assert scheduler._enqueue(state, request, cfg)
    assert state.data['pending'][0]['lot_id'] == 'L2,L3'
    calls = []
    monkeypatch.setattr(scheduler, 'run_main', lambda c, a, l, email_receiver=None: calls.append(a) or 0)
    scheduler.process_triggers(cfg, state)
    assert calls == ['_TRIGGER_V_L2,L3_S1,S1']
    assert {'V|L2|S1', 'V|L3|S1'} <= set(state.data['done_targets'])


def test_governor_slots_are_shared_between_processes(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_SLOT_DIR', str(tmp_path / 'slots'))
    monkeypatch.setattr(governor, 'usable_cores', lambda: 5)
    monkeypatch.setattr(governor, 'busy_cores', lambda cores, sample_sec=0.25: 0.0)
    monkeypatch.setattr(governor, 'available_memory_gb', lambda: 64.0)
    settings = dict(parallel_reserve_cores=1, parallel_replan_sec=0)
    governor.release_all()
    try:
        first = governor.plan_workers(settings, force=True)
        assert first['workers'] == 4 and governor.held_slots() == 4
        # 다른 프로세스는 남은 슬롯만 얻는다 → 서버 전체 합계가 코어-예비를 넘지 않는다.
        code = textwrap.dedent(f'''
            import os, sys; sys.path.insert(0, {str(ROOT)!r})
            import Scheduler  # 설치본의 ZIP 모듈 경로를 실제 진입점에서 준비한다.
            os.environ['AUTO_REPORT_SLOT_DIR'] = {str(tmp_path / 'slots')!r}
            import resource_governor as g
            g.usable_cores = lambda: 5; g.busy_cores = lambda c, sample_sec=0.25: 0.0; g.available_memory_gb = lambda: 64.0
            print(g.plan_workers(dict(parallel_reserve_cores=1, parallel_workers=100), force=True)['workers'])
        ''')
        other = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        assert other.stdout.strip() == '1', other.stderr
        governor.trim_lease(2)
        other = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        assert other.stdout.strip() == '2', other.stderr
    finally:
        governor.release_all()


def test_governor_shrinks_under_cpu_or_memory_pressure(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_SLOT_DIR', str(tmp_path / 'slots'))
    monkeypatch.setattr(governor, 'usable_cores', lambda: 8)
    settings = dict(parallel_reserve_cores=1, parallel_replan_sec=0, parallel_mem_per_worker_gb=1.0, parallel_reserve_gb=3.0)
    try:
        monkeypatch.setattr(governor, 'busy_cores', lambda cores, sample_sec=0.25: 4.0)   # S3 전송·다른 작업
        monkeypatch.setattr(governor, 'available_memory_gb', lambda: 58.0)
        assert governor.plan_workers(settings, force=True)['workers'] == 3
        governor.release_all()
        monkeypatch.setattr(governor, 'busy_cores', lambda cores, sample_sec=0.25: 0.0)
        monkeypatch.setattr(governor, 'available_memory_gb', lambda: 5.5)
        assert governor.plan_workers(settings, force=True)['workers'] == 2
        governor.release_all()
        monkeypatch.setattr(governor, 'available_memory_gb', lambda: 3.5)
        plan = governor.plan_workers(settings, force=True)
        assert plan['workers'] == 1 and governor.held_slots() == 0 and '메모리' in plan['reason']
    finally:
        governor.release_all()


def test_forced_workers_are_honored(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_SLOT_DIR', str(tmp_path / 'slots'))
    monkeypatch.setattr(governor, 'usable_cores', lambda: 8)
    monkeypatch.setattr(governor, 'busy_cores', lambda cores, sample_sec=0.25: 0.0)
    monkeypatch.setattr(governor, 'available_memory_gb', lambda: 64.0)
    try:
        assert governor.plan_workers(dict(parallel_workers=3), force=True)['workers'] == 3
    finally:
        governor.release_all()


def test_requested_workers_cannot_bypass_resource_or_slot_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_SLOT_DIR', str(tmp_path / 'slots'))
    monkeypatch.setattr(governor, 'usable_cores', lambda: 4)
    monkeypatch.setattr(governor, 'busy_cores', lambda cores, sample_sec=0.25: 0.0)
    monkeypatch.setattr(governor, 'available_memory_gb', lambda: 64.0)
    try:
        assert governor.plan_workers(dict(parallel_workers=100), force=True)['workers'] == 3
        governor.release_all()
        monkeypatch.setattr(governor, 'available_memory_gb', lambda: 3.5)
        assert governor.plan_workers(dict(parallel_workers=100), force=True)['workers'] == 1
        assert governor.held_slots() == 0
    finally:
        governor.release_all()


def test_async_uploads_update_records_from_main_thread(monkeypatch):
    saved = []
    monkeypatch.setattr(main, 'ops_put', lambda kind, key, value: saved.append((kind, key, dict(value))))
    monkeypatch.setattr(main, 'print_status', lambda *a, **k: None)
    monkeypatch.setattr(main, '_RUN', type('R', (), {'data': {'issues': []}})())
    gate = threading.Event()

    class Client:
        def upload_file(self, local, bucket, key):
            gate.wait(5)
            if 'bad' in key:
                raise OSError('network')

    ok, bad = {'id': 'r1', 'upload': 'sending'}, {'id': 'r2', 'upload': 'sending'}
    main._upload_async(Client(), 'a.pptx', 'b', 'good.pptx', ok, 'L1_S1')
    main._upload_async(Client(), 'b.pptx', 'b', 'bad.pptx', bad, 'L2_S1')
    main._drain_uploads(block=False)
    assert saved == [] and ok['upload'] == 'sending'      # 전송 중에는 기다리지 않는다
    gate.set()
    main._drain_uploads(block=True)
    assert (ok['upload'], bad['upload']) == ('success', 'failed')
    assert main._RUN.data['issues'] == ['S3 업로드 실패: L2_S1']
    assert [k for _, k, _ in saved] == ['r1', 'r2'] and main._UPLOADS == []


def test_product_lock_waits_for_other_process_then_proceeds(tmp_path):
    path = str(tmp_path / 'p.lock')
    code = textwrap.dedent(f'''
        import sys, time; sys.path.insert(0, {str(ROOT)!r})
        import Scheduler  # compact 설치의 보조 모듈 경로 준비
        import My_Function as mf
        with mf.process_lock({path!r}):
            print('held', flush=True); time.sleep(3)
    ''')
    holder = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(RuntimeError):
            with mf.process_lock(path):
                pass
        import time
        started = time.time()
        with mf.process_lock(path, wait_sec=30, poll_sec=0.2):
            assert time.time() - started >= 1.5     # 실패하지 않고 앞 작업이 끝날 때까지 기다렸다
    finally:
        holder.wait(timeout=30)
