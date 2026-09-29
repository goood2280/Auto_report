"""Installer upgrade cleanup only touches retired AI artifacts, using isolated fake bundles."""
import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path):
    spec = importlib.util.spec_from_file_location('isolated_builder', ROOT / 'gen_setup.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    scope = {'__name__': 'isolated_setup', '__file__': str(tmp_path / 'setup.py'),
             'RUNTIME_FILES': builder.RUNTIME_FILES, 'RETIRED_FILES': builder.RETIRED_FILES}
    exec(compile(builder.INSTALLER, '<installer>', 'exec'), scope)
    bundle = {name: '# isolated test source\n' for name in builder.ENTRY_FILES + builder.RUNTIME_FILES}
    bundle['gen_setup.py'] = '# isolated builder\n'
    scope['read_bundle'] = lambda: dict(bundle)
    return scope


def put(root, name, content='preserve'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def test_upgrade_removes_ai_outputs_and_bytecode_but_preserves_operational_files(installer, tmp_path):
    root = tmp_path / 'upgrade'
    retired = [
        '__pycache__/Manager.cpython-310.pyc', '__pycache__/manager_llm.cpython-314.pyc',
        '__pycache__/gpt_oss_client.cpython-310.pyc', 'ml_threshold_tuner.pyc',
        'RUN/AI/ai_input_L1_S1.json', 'RUN/AI/ai_input_L1_S1.md',
        'RUN/AI/anomaly_rule_check_L1_S1.json', 'RUN/AI/anomaly_rule_check_L1_S1.txt',
        'RUN/AI/nl_rules_json.json', 'RUN/AI/nl_rules_compiled.json', 'RUN/AI/nl_rules_map.json',
        'RUN/AI/rule_digest_20260713.txt', 'RUN/AI/rule_digest_state.json',
    ]
    protected = [
        'RUN/DB/vehicle_daily/date=2026-09-30/data.parquet', 'RUN/OPS/operations.sqlite',
        'RUN/OPS/mlmode/influence.json', 'RUN/OPS/config_backups/My_config.py.bak',
        'RUN/QUEUE/trigger_queue.jsonl', 'RUN/QUEUE/scheduler_state.json',
        'RUN/log/vehicle_et_log.csv', 'RUN/Report/report.html',
        'reformatter/config.yaml', 'reformatter/scheduler.yaml', '.env',
        '__pycache__/My_Function.cpython-310.pyc', '__pycache__/anomaly_engine.cpython-314.pyc',
        'RUN/AI/operator-note.txt', 'RUN/AI/ai_input_L1_S1.json.bak',
        'RUN/AI/rule_digest_custom.txt', 'RUN/AI/ai_input_nested.json/keep.txt',
    ]
    for name in retired + protected:
        put(root, name)
    put(root, 'gpt_oss_client.py', '# retired local mock\n')
    installer['install'](root)
    assert all(not (root / name).exists() for name in retired)
    assert all((root / name).read_text(encoding='utf-8') == 'preserve' for name in protected)
    assert not (root / 'gpt_oss_client.py').exists()
    backup = list((root / '.setup-backups').glob('*/gpt_oss_client.py.bak'))
    assert len(backup) == 1
    assert backup[0].read_text(encoding='utf-8') == '# retired local mock\n'
    installer['install'](root)
    assert all((root / name).read_text(encoding='utf-8') == 'preserve' for name in protected)
    assert len(list((root / '.setup-backups').glob('*/gpt_oss_client.py.bak'))) == 1


def test_empty_retired_ai_folder_removed(installer, tmp_path):
    root = tmp_path / 'upgrade'
    put(root, 'RUN/AI/nl_rules_json.json')
    installer['install'](root)
    assert not (root / 'RUN/AI').exists()
    assert (root / 'RUN').is_dir()


@pytest.mark.parametrize('directory', ['__pycache__', 'RUN', 'RUN/AI'])
def test_cleanup_skips_link_or_junction_directories(installer, tmp_path, directory, monkeypatch):
    root = tmp_path / 'upgrade'
    bytecode = put(root, '__pycache__/manager_llm.cpython-310.pyc')
    output = put(root, 'RUN/AI/ai_input_L1_S1.json')
    linked = root / directory
    monkeypatch.setitem(installer, '_is_link', lambda path: path == linked)
    installer['_cleanup_retired_artifacts'](root)
    assert bytecode.exists() == (directory == '__pycache__')
    assert output.exists() == (directory in {'RUN', 'RUN/AI'})


def test_cleanup_skips_linked_files(installer, tmp_path, monkeypatch):
    root = tmp_path / 'upgrade'
    linked = {
        put(root, '__pycache__/manager_llm.cpython-310.pyc'),
        put(root, 'RUN/AI/ai_input_L1_S1.json'),
    }
    monkeypatch.setitem(installer, '_is_link', lambda path: path in linked)
    assert installer['_cleanup_retired_artifacts'](root) == 0
    assert all(path.exists() for path in linked)


def test_link_detection_includes_windows_reparse_points(installer):
    path = SimpleNamespace(is_symlink=lambda: False,
                           lstat=lambda: SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))
    assert installer['_is_link'](path)
    path.lstat = lambda: SimpleNamespace(st_file_attributes=0)
    assert not installer['_is_link'](path)
