"""Durable serial queue regressions; Main, mail, DB and corporate APIs stay offline."""
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import Scheduler as scheduler


@pytest.fixture
def queue(tmp_path, monkeypatch):
    scheduler._STOP.clear()
    monkeypatch.setattr(scheduler, '_LOG_PATH', None)
    monkeypatch.setattr(scheduler, 'log', lambda *args: None)
    monkeypatch.setattr(scheduler, 'heartbeat', lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, 'known_vehicles', lambda: {'TEST'})
    cfg = dict(scheduler=dict(main_timeout_sec=10, execution_lock_wait_sec=0),
               watchdog=dict(ops_root=str(tmp_path / 'ops')),
               trigger=dict(queue_root=str(tmp_path / 'queue'), inbox_dir=str(tmp_path / 'queue/inbox'),
                            queue_file=str(tmp_path / 'queue/requests.jsonl'),
                            email_receiver=['OPS'], allowed_email_receiver=['OPS']))
    state = scheduler.State(str(tmp_path / 'queue/state.json'))
    yield cfg, state
    scheduler._STOP.clear()


def request(identity='req-1', lot='L1', **changes):
    req, why = scheduler._norm_request(dict(req_id=identity, vehicle='TEST', lot_id=lot,
                                          step_id='S1', generate_only=True, **changes), 'test')
    assert req is not None, why
    return req


def test_claim_is_durable_before_main_and_finish_clears_it(queue, monkeypatch):
    cfg, state = queue
    state.data['pending'] = [request()]
    def main(*args, **kwargs):
        saved = json.loads(Path(state.path).read_text(encoding='utf-8'))
        assert saved['pending'] == []
        assert saved['active']['request']['req_id'] == 'req-1'
        assert saved['active']['run_id'] == cfg['_run_id']
        return 0
    monkeypatch.setattr(scheduler, 'run_main', main)
    assert scheduler.process_triggers(cfg, state) == 1
    assert state.data['active'] is None
    assert state.data['history']['id:req-1']['status'] == 'done'
    assert state.data['history']['id:req-1']['run_id']
    assert not {'_run_id', '_state', '_generate_only', '_request_id'} & cfg.keys()


def active_claim(cfg, state, result=None, pid=None):
    path = Path(cfg['watchdog']['ops_root']) / 'results/run-1.json'
    state.data['active'] = dict(request=request(), run_id='run-1', result_path=str(path), child_pid=pid)
    state.save()
    if result is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding='utf-8')


@pytest.mark.parametrize(('result', 'expected'), [
    ({'id': 'run-1', 'status': 'success'}, 'done'),
    ({'id': 'run-1', 'status': 'failed'}, 'failed'),
    ({'id': 'wrong-run', 'status': 'success'}, 'unknown'),
    (None, 'unknown'),
])
def test_restart_resolves_claim_without_relaunch(queue, monkeypatch, result, expected):
    cfg, state = queue
    active_claim(cfg, state, result)
    state = scheduler.State(state.path)
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: pytest.fail('Recovered request relaunched'))
    scheduler.recover_active(cfg, state)
    assert state.data['history']['id:req-1']['status'] == expected
    assert not scheduler._enqueue(state, request(), cfg)
    assert state.data['active'] is None


def test_restart_does_not_overlap_surviving_child(queue, monkeypatch):
    cfg, state = queue
    active_claim(cfg, state, pid=123)
    monkeypatch.setattr(scheduler, '_pid_alive', lambda pid: pid == 123)
    with pytest.raises(RuntimeError, match='pid=123'):
        scheduler.recover_active(cfg, state)
    assert state.data['active']['request']['req_id'] == 'req-1'


@pytest.mark.parametrize('permission_error', [False, True])
def test_restart_does_not_overlap_orphan_posix_workers(queue, monkeypatch, permission_error):
    cfg, state = queue
    active_claim(cfg, state, pid=123)
    monkeypatch.setattr(scheduler, '_pid_alive', lambda pid: False)
    def probe(pid, signum):
        assert pid == 123 and signum == 0
        if permission_error:raise PermissionError('group ownership unknown')
    monkeypatch.setattr(scheduler, 'os', SimpleNamespace(name='posix', killpg=probe))
    with pytest.raises(RuntimeError, match='pgid=123'):
        scheduler.recover_active(cfg, state)
    assert state.data['active']['request']['req_id'] == 'req-1'


def test_failed_claim_write_prevents_main_launch(queue, monkeypatch):
    cfg, state = queue
    state.data['pending'] = [request()]
    monkeypatch.setattr(state, 'save', lambda: (_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: pytest.fail('Unclaimed request launched'))
    with pytest.raises(OSError, match='disk full'):
        scheduler.process_triggers(cfg, state)


def test_corrupt_state_is_not_silently_reset(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{bad json', encoding='utf-8')
    with pytest.raises(RuntimeError, match='중복 실행 방지'):
        scheduler.State(str(path))
    assert path.read_text(encoding='utf-8') == '{bad json'


def test_repeated_poll_keeps_pending_inbox_until_execution_finishes(queue, monkeypatch):
    cfg, state = queue
    assert scheduler.cmd_enqueue(cfg, json.dumps(request())) == 0
    path = Path(cfg['trigger']['inbox_dir']) / 'req-1.json'
    assert scheduler.collect_requests(cfg, state) == 1
    assert scheduler.collect_requests(cfg, state) == 0
    assert path.exists()
    monkeypatch.setattr(scheduler, 'run_main', lambda *args, **kwargs: 0)
    scheduler.process_triggers(cfg, state)
    assert not path.exists()
    assert (path.parent.parent / 'done/req-1.json').exists()


def test_partial_jsonl_keeps_byte_offset_then_accepts_completed_utf8_line(queue):
    cfg, state = queue
    path = Path(cfg['trigger']['queue_file'])
    path.parent.mkdir(parents=True)
    payload = dict(request(), note='한국어 요청')
    path.write_bytes(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    assert scheduler.collect_requests(cfg, state) == 0
    assert state.data['queue_offset'] == 0
    with path.open('ab') as stream:stream.write(b'\r\n')
    assert scheduler.collect_requests(cfg, state) == 1
    assert state.data['queue_offset'] == path.stat().st_size
    assert scheduler.collect_requests(cfg, state) == 0


def test_queue_capacity_leaves_overflow_in_inbox_and_jsonl(queue):
    cfg, state = queue
    cfg['trigger']['max_pending'] = 2
    for number in range(3):
        assert scheduler.cmd_enqueue(cfg, json.dumps(request(f'r{number}', f'L{number}'))) == 0
    path = Path(cfg['trigger']['queue_file'])
    path.write_text(json.dumps(request('jsonl', 'OTHER'))+'\n', encoding='utf-8')
    assert scheduler.collect_requests(cfg, state) == 2
    assert len(state.data['pending']) == 2
    assert state.data['queue_offset'] == 0
    assert (Path(cfg['trigger']['inbox_dir']) / 'r2.json').exists()


def test_backoff_request_does_not_block_a_ready_request(queue, monkeypatch):
    cfg, state = queue
    delayed = request('later', 'LATER')
    delayed['next_attempt_ts'] = time.time() + 60
    state.data['pending'] = [delayed, request('now', 'NOW')]
    seen = []
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: seen.append(cfg['_request_id']) or 0)
    assert scheduler.process_triggers(cfg, state) == 1
    assert seen == ['id:now']
    assert state.data['pending'] == [delayed]


@pytest.mark.parametrize('rc', [-2, -9, -10, 1])
def test_ambiguous_or_completed_failure_never_automatically_retries(queue, monkeypatch, rc):
    cfg, state = queue
    cfg['trigger']['max_retry'] = 99
    state.data['pending'] = [request()]
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: rc)
    scheduler.process_triggers(cfg, state)
    assert not state.data['pending']
    assert state.data['history']['id:req-1']['status'] == ('failed' if rc == 1 else 'unknown')


@pytest.mark.parametrize('receiver', [['WRONG'], [], ['OPS', 'WRONG']])
def test_receiver_error_never_falls_back_to_default(queue, monkeypatch, receiver):
    cfg, state = queue
    req = request(email_receiver=receiver)
    req['generate_only'] = False
    assert scheduler.cmd_enqueue(cfg, json.dumps(req)) == 2
    state.data['pending'] = [req]
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: pytest.fail('Wrong recipient launched'))
    scheduler.process_triggers(cfg, state)
    assert state.data['history']['id:req-1']['rc'] == -4


@pytest.mark.parametrize('changes', [dict(force='false'), dict(req_id='../escape'), dict(req_id='C:\\escape'), dict(email_receiver={})])
def test_unsafe_request_types_or_paths_are_rejected(changes):
    raw = dict(vehicle='TEST', lot_id='L', step_id='S')
    raw.update(changes)
    req, why = scheduler._norm_request(raw, 'test')
    assert req is None and why


def test_producers_use_unique_ids_and_do_not_overwrite_same_id(queue):
    cfg, state = queue
    for lot in ('L1', 'L2'):
        assert scheduler.cmd_enqueue(cfg, json.dumps(dict(vehicle='TEST', lot_id=lot, step_id='S', generate_only=True))) == 0
    assert len(list(Path(cfg['trigger']['inbox_dir']).glob('*.json'))) == 2
    assert scheduler.cmd_enqueue(cfg, json.dumps(request('fixed', 'ORIGINAL'))) == 0
    assert scheduler.cmd_enqueue(cfg, json.dumps(request('fixed', 'REPLACED'))) == 2
    path = Path(cfg['trigger']['inbox_dir']) / 'fixed.json'
    assert json.loads(path.read_text(encoding='utf-8'))['lot_id'] == 'ORIGINAL'
    assert not list(path.parent.glob('*.tmp'))


def test_status_commands_leave_missing_config_and_runtime_absent(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / 'missing/scheduler.yaml'
    monkeypatch.setattr(scheduler, 'BASE_DIR', str(tmp_path))
    for options in (['--status'], ['--request-status', 'missing-id']):
        monkeypatch.setattr(sys, 'argv', ['Scheduler.py', '--config', str(cfg_path), *options])
        assert scheduler.main() == 0
    assert not cfg_path.exists() and not (tmp_path / 'RUN').exists()
    assert 'not_found' in capsys.readouterr().out


def test_read_only_status_cannot_be_mixed_with_service_execution(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['Scheduler.py', '--status', '--daily-trend-once'])
    with pytest.raises(SystemExit) as stopped:
        scheduler.main()
    assert stopped.value.code == 2


def test_main_total_timeout_and_environment_include_execution_wait(queue, monkeypatch):
    cfg, state = queue
    cfg['scheduler'].update(main_timeout_sec=5, execution_lock_wait_sec=11)
    captured = {}
    class Process:
        pid = 123
        stdout = io.BytesIO(b'fake offline Main\n')
        def wait(self, timeout):captured['timeout'] = timeout;return 0
        def poll(self):return 0
    def launch(args, **kwargs):
        captured.update(kwargs)
        env = kwargs['env']
        path = Path(env['AUTO_REPORT_RESULT_PATH'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(id=env['AUTO_REPORT_RUN_ID'], status='success')), encoding='utf-8')
        return Process()
    monkeypatch.setattr(scheduler.subprocess, 'Popen', launch)
    monkeypatch.setattr(scheduler, '_kill_tree', lambda proc: None)
    assert scheduler.run_main(cfg, 'TEST', 'offline') == 0
    assert captured['env']['AUTO_REPORT_EXECUTION_WAIT_SEC'] == '11'
    # The first wait slice is capped at 15s and has the full 16s total budget.
    assert 14.9 <= captured['timeout'] <= 15


def test_manual_request_arriving_during_product_runs_before_next_product(queue, monkeypatch):
    cfg, state = queue
    cfg['scheduler'].update(groups=[dict(name='A', every=1, products=['TEST', 'TEST'])], product_gap_sec=0)
    seen = []
    def main(cfg, argument, label, **kwargs):
        seen.append(argument)
        if len(seen) == 1:
            assert scheduler.cmd_enqueue(cfg, json.dumps(request())) == 0
        return 0
    monkeypatch.setattr(scheduler, 'run_main', main)
    monkeypatch.setattr(scheduler, 'export_lot_history', lambda *a: None)
    scheduler.run_cycle(cfg, state)
    assert seen == ['TEST', '_TRIGGER_TEST_L1_S1', 'TEST']
    assert state.cycle == 1


def test_missing_vehicle_config_blocks_producer_and_consumer(queue, monkeypatch):
    cfg, state = queue
    monkeypatch.setattr(scheduler, 'known_vehicles', lambda: set())
    assert scheduler.cmd_enqueue(cfg, json.dumps(request())) == 2
    state.data['pending'] = [request()]
    monkeypatch.setattr(scheduler, 'run_main', lambda *a, **k: pytest.fail('Unverified vehicle launched'))
    scheduler.process_triggers(cfg, state)
    assert state.data['history']['id:req-1']['rc'] == -3


def test_waiting_execution_health_respects_its_own_budget(queue, monkeypatch):
    cfg, state = queue
    cfg['scheduler']['execution_lock_wait_sec'] = 10800
    cfg['watchdog'].update(stale_sec=180, progress_stale_sec=1800)
    monkeypatch.setattr(scheduler, '_read_json', lambda *a: dict(stage='waiting_execution', updated=100))
    beat = dict(phase='main', updated=4000, run_id='offline')
    health = scheduler.watchdog_health(cfg, beat, now=4000)
    assert health['state'] == 'healthy' and health['message'] == '공통 실행 잠금 대기'
    monkeypatch.setattr(scheduler, '_read_json', lambda *a: dict(stage='render', updated=100))
    assert scheduler.watchdog_health(cfg, beat, now=4000)['state'] == 'slow'


def test_daily_service_includes_wait_budget_and_always_cleans_child(queue, monkeypatch):
    cfg, state = queue
    cfg['scheduler']['execution_lock_wait_sec'] = 7
    captured = {}
    proc = SimpleNamespace(wait=lambda timeout: captured.update(timeout=timeout) or 0, poll=lambda: 0)
    monkeypatch.setenv('AUTO_REPORT_REQUEST_ID', 'inherited-wrong-request')
    monkeypatch.setattr(scheduler.subprocess, 'Popen', lambda *a, **kw: captured.update(kw) or proc)
    monkeypatch.setattr(scheduler, '_kill_tree', lambda child: captured.update(cleaned=child is proc))
    assert scheduler._run_service_main(cfg, ['--daily-trend-report', 'offline.json'], 12) == 0
    assert captured['timeout'] == 19
    assert captured['env']['AUTO_REPORT_EXECUTION_WAIT_SEC'] == '7'
    assert 'AUTO_REPORT_REQUEST_ID' not in captured['env']
    assert captured['cleaned'] and scheduler._CURRENT_PROC is None


def test_posix_cleanup_includes_workers_after_parent_has_exited(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, 'os', SimpleNamespace(name='posix', killpg=lambda pid, sig: calls.append((pid, sig))))
    monkeypatch.setattr(scheduler, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9))
    proc = SimpleNamespace(pid=321, poll=lambda: 1, wait=lambda timeout: 1)
    scheduler._kill_tree(proc)
    assert calls == [(321, 15), (321, 9)]
