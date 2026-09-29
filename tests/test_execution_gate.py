"""Shared execution gate tests without corporate query, reports or delivery."""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import Main as main


@pytest.mark.parametrize('fails', [False, True])
def test_workers_and_uploads_finish_before_gate_release(monkeypatch, tmp_path, fails):
    events = []

    @contextmanager
    def lock(path, wait_sec):
        assert path == str(tmp_path / 'locks' / 'executor.lock')
        assert wait_sec == 42
        events.append('locked')
        try:
            yield
        finally:
            events.append('released')

    monkeypatch.setattr(main, 'operations_root', lambda: str(tmp_path))
    monkeypatch.setattr(main, 'process_lock', lock)
    monkeypatch.setenv('AUTO_REPORT_EXECUTION_WAIT_SEC', '42')
    monkeypatch.setattr(main, '_drain_uploads', lambda block: events.append('uploads'))
    monkeypatch.setattr(main, 'shutdown_chart_pool', lambda: events.append('workers'))

    def action():
        events.append('action')
        if fails:
            raise RuntimeError('test failure')
        return 123

    if fails:
        with pytest.raises(RuntimeError, match='test failure'):
            main._execute_serially(action)
    else:
        assert main._execute_serially(action) == 123
    assert events == ['locked', 'action', 'uploads', 'workers', 'released']


@pytest.mark.parametrize('wait', ['-1', 'nan', 'inf'])
def test_invalid_wait_budget_rejected_before_execution(monkeypatch, wait):
    monkeypatch.setenv('AUTO_REPORT_EXECUTION_WAIT_SEC', wait)
    with pytest.raises(ValueError):
        main._execution_wait_sec()


def test_daily_and_ml_entry_use_same_gate(monkeypatch, tmp_path):
    request = tmp_path / 'daily.json'
    request.write_text(json.dumps({'service': 'mlmode'}), encoding='utf-8')
    events = []
    monkeypatch.setattr(sys, 'argv', ['Main.py', '--daily-trend-report', str(request)])
    monkeypatch.setattr(main, '_daily_trend_report',
                        lambda req: events.append(req['service']) or {'status': 'preview'})

    def gate(action):
        events.append('gate')
        return action()

    monkeypatch.setattr(main, '_execute_serially', gate)
    assert main.main() == 0
    assert events == ['gate', 'mlmode']


def test_ml_evaluation_cannot_overlap_scheduler_analysis(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(sys, 'argv', ['Main.py', '--mlmode-evaluate'])
    monkeypatch.setattr(main, 'operations_root', lambda: str(tmp_path))
    monkeypatch.setattr(main, 'mlmode_evaluate',
                        lambda cfg, dest: events.append('evaluation') or {'ensemble': 'ok'})

    def gate(action):
        events.append('gate')
        return action()

    monkeypatch.setattr(main, '_execute_serially', gate)
    assert main.main() == 0
    assert events == ['gate', 'evaluation']


def test_separate_main_processes_wait_until_previous_cleanup(tmp_path):
    env = dict(os.environ, AUTO_REPORT_OPS_ROOT=str(tmp_path / 'ops'),
               AUTO_REPORT_EXECUTION_WAIT_SEC='20', PYTHONIOENCODING='utf-8')
    first_code = '''
import time
from pathlib import Path
import Main as m
root = Path(m.operations_root())
m._drain_uploads = lambda block: None
def cleanup():
    time.sleep(2)
    (root / 'cleanup').write_text(str(time.monotonic()))
m.shutdown_chart_pool = cleanup
def action():
    print('GATE_HELD', flush=True)
m._execute_serially(action)
'''
    second_code = '''
import time
from pathlib import Path
import Main as m
root = Path(m.operations_root())
m._drain_uploads = lambda block: None
m.shutdown_chart_pool = lambda: None
def action():
    assert (root / 'cleanup').exists(), 'Started before previous cleanup'
    (root / 'next').write_text(str(time.monotonic()))
m._execute_serially(action)
'''
    first = subprocess.Popen([sys.executable, '-c', first_code], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8')
    try:
        for line in first.stdout:
            if line.strip() == 'GATE_HELD':
                break
        else:
            pytest.fail('First process did not acquire execution gate')
        second = subprocess.run([sys.executable, '-c', second_code], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert second.returncode == 0, second.stdout + second.stderr
        assert first.wait(timeout=30) == 0
        assert float((tmp_path / 'ops' / 'next').read_text()) >= float((tmp_path / 'ops' / 'cleanup').read_text())
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=30)
