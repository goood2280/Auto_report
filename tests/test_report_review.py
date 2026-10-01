"""Local candidate workflow tests; all delivery functions and child commands are mocked."""
import json
import os
from contextlib import nullcontext
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_review as rr


@pytest.fixture(autouse=True)
def restore_worker_environment():
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def source_tree(tmp_path):
    source = tmp_path / "installed"
    source.mkdir()
    for name in rr.COPY_FILES:
        (source / name).write_text("# " + name + "\n", encoding="utf-8")
    with zipfile.ZipFile(source / 'auto_report_runtime.zip', 'w') as archive:
        archive.writestr('runtime_versions.py', '# test infrastructure')
    for name in rr.DOC_FILES:
        (source / name).write_text("local docs\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "offline.py").write_text("def test_offline(): pass\n", encoding="utf-8")
    (source / "reformatter").mkdir()
    (source / "reformatter" / "report_items.yaml").write_text("selected: []\n", encoding="utf-8")
    (source / ".env").write_text("secret=should-not-copy\n", encoding="utf-8")
    (source / "RUN" / "OPS").mkdir(parents=True)
    (source / "RUN" / "OPS" / "private.json").write_text("private", encoding="utf-8")
    (source / "RUN" / "QUEUE").mkdir(parents=True)
    (source / "RUN" / "QUEUE" / "state.json").write_text("state", encoding="utf-8")
    (source / "RUN" / "DB").mkdir(parents=True)
    (source / "RUN" / "DB" / "fixture.parquet").write_bytes(b"fixture")
    (source / "RUN" / "log").mkdir(parents=True)
    (source / "RUN" / "log" / "TEST_et_log.csv").write_text("lot\n", encoding="utf-8")
    return source


def candidate(tmp_path, inputs=()):
    source = source_tree(tmp_path)
    target = tmp_path / "candidate"
    root = rr.prepare(str(source), str(target), list(inputs))
    return source, root


def pass_check(root):
    rr._json_write(root / rr.REVIEW_DIR / "check.json", {
        "status": "passed", "fingerprint": rr._fingerprint(root),
        "report_items_hash": rr._selection_hash(root), "tests": ["tests/offline.py"],
    })


def artifacts(root):
    (root / "RUN" / "Report").mkdir(parents=True, exist_ok=True)
    html_file = root / "RUN" / "Report" / "review.html"
    html_file.write_text("<!doctype html><html><body><p>review</p></body></html>", encoding="utf-8")
    ppt_file = root / "RUN" / "Report" / "review.pptx"
    with zipfile.ZipFile(ppt_file, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return html_file, ppt_file


def make_receipt(root, monkeypatch):
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    pass_check(root)
    html_file, ppt_file = artifacts(root)
    return rr.record(str(html_file), str(ppt_file), "TEST", "Review sample")


def test_prepare_copies_only_owned_files_and_explicit_inputs(tmp_path):
    source = source_tree(tmp_path)
    target = tmp_path / "candidate"
    root = rr.prepare(str(source), str(target), ["reformatter/report_items.yaml", "RUN/DB/fixture.parquet"])
    assert (root / "Main.py").is_file()
    assert (root / "reformatter" / "report_items.yaml").is_file()
    assert (root / "RUN" / "DB" / "fixture.parquet").read_bytes() == b"fixture"
    assert not (root / ".env").exists()
    assert not (root / "RUN" / "OPS").exists()
    assert not (root / "RUN" / "QUEUE").exists()
    assert not (root / "RUN" / "log").exists()
    assert json.loads((root / rr.MANIFEST).read_text(encoding="utf-8"))["source"] == str(source.resolve())


@pytest.mark.parametrize('change_during_check', [False, True])
def test_check_isolated_receipt_rejects_code_changed_during_tests(tmp_path, monkeypatch, change_during_check):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, '_candidate_root', lambda: root)

    def run(args, **kwargs):
        assert args[-1] == 'tests/offline.py'
        assert kwargs['cwd'] == root
        assert kwargs['env']['PYTHON_DOTENV_DISABLED'] == '1'
        assert Path(kwargs['env']['AUTO_REPORT_OPS_ROOT']).is_relative_to(root / '.review')
        if change_during_check:
            (root / 'Main.py').write_text('# changed while tests were running\n', encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rr.subprocess, 'run', run)
    assert rr.check(['tests/offline.py']) == (2 if change_during_check else 0)
    receipt = json.loads((root / '.review/check.json').read_text(encoding='utf-8'))
    assert receipt['status'] == ('failed' if change_during_check else 'passed')
    assert receipt['source_changed_during_check'] == change_during_check


@pytest.mark.parametrize("unsafe", [".env", "RUN/OPS/private.json", "RUN/QUEUE/state.json", "../outside.csv"])
def test_prepare_rejects_non_allowlisted_inputs(tmp_path, unsafe):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError):
        rr.prepare(str(source), str(tmp_path / "candidate"), [unsafe])
    assert not (tmp_path / "candidate").exists()


def test_prepare_rejects_symlink_input(tmp_path):
    source = source_tree(tmp_path)
    link = source / "reformatter" / "linked.yaml"
    try:
        link.symlink_to(source / "reformatter" / "report_items.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="Symlink"):
        rr.prepare(str(source), str(tmp_path / "candidate"), ["reformatter/linked.yaml"])


def test_record_rejects_stale_check_and_receipt_artifact_change(tmp_path, monkeypatch):
    _, root = candidate(tmp_path, ["reformatter/report_items.yaml"])
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    html_file, ppt_file = artifacts(root)
    pass_check(root)
    (root / "Main.py").write_text("# changed after test\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale or failed"):
        rr.record(str(html_file), str(ppt_file), "TEST", "Review")

    review_id = make_receipt(root, monkeypatch)
    html_file.write_text("<!doctype html><html>changed</html>", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact has changed"):
        rr._read_receipt(root, review_id)


def test_send_sample_targets_only_requested_user_and_records_unknown(tmp_path, monkeypatch):
    _, root = candidate(tmp_path, ["reformatter/report_items.yaml"])
    review_id = make_receipt(root, monkeypatch)
    monkeypatch.setattr(rr, "__file__", str(root / "report_review.py"))
    calls = []
    fake_config = SimpleNamespace(base_path=str(root), settings={"use_s3_upload": True},
                                  load_from_yaml=lambda vehicle: None,
                                  get=lambda key, default=None: default)
    main = ModuleType("Main")
    main.__file__ = str(root / "Main.py")
    main.GLOBAL_CONFIG = fake_config
    main._durable_mail = lambda identity, recipients, title, html, ppt, config: calls.append(
        (identity, recipients, title, html, ppt, config)) or "unknown"
    helper = ModuleType("My_Function")
    helper.__file__ = str(root / "My_Function.py")
    helper.samsung_email = lambda user: user + "@samsung.com"
    helper.email_receivers = lambda addresses: [{"email": addresses[0], "recipientType": "TO", "seq": 1}]
    helper.process_lock = lambda *a, **k: nullcontext()
    monkeypatch.setitem(sys.modules, "Main", main)
    monkeypatch.setitem(sys.modules, "My_Function", helper)
    assert rr.send_sample(review_id, ["reviewer.id"]) == 2
    identity, recipients, title, html, ppt, config = calls[0]
    assert recipients == [{"email": "reviewer.id@samsung.com", "recipientType": "TO", "seq": 1}]
    assert title == "TEST · Review sample"
    assert Path(html).is_relative_to(root) and Path(ppt).is_relative_to(root)
    assert config.settings["use_s3_upload"] is False
    sent = root / rr.REVIEW_DIR / "sends" / f"{review_id}-reviewer.id.json"
    assert json.loads(sent.read_text(encoding="utf-8"))["status"] == "unknown"


def test_promotion_plan_blocks_production_source_drift(tmp_path, monkeypatch):
    source, root = candidate(tmp_path, ["reformatter/report_items.yaml"])
    review_id = make_receipt(root, monkeypatch)
    (source / "Main.py").write_text("# production drift\n", encoding="utf-8")
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: pytest.fail("must block before build"))
    with pytest.raises(ValueError, match="source drifted"):
        rr.promotion_plan(review_id)


def test_preview_dispatch_uses_candidate_worker_and_isolated_environment(tmp_path, monkeypatch):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    calls = []
    monkeypatch.setattr(rr.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert rr.preview("TEST", "LOT.1", "STEP-1", "auto") == 0
    args, kwargs = calls[0]
    assert args[1] == str(root / "report_review.py")
    assert args[2] == "_preview_worker"
    assert kwargs["cwd"] == root
    assert kwargs["env"]["AUTO_REPORT_OPS_ROOT"] == str(root / "RUN" / "OPS")
    assert kwargs["env"]["AUTO_REPORT_GENERATE_ONLY"] == "1"


def fake_config(root, external_path=None):
    settings = {}
    if external_path:
        settings["inline_file_path"] = str(external_path)
    def load(vehicle, yaml_path=None):
        settings["vehicle"] = vehicle
    return SimpleNamespace(base_path=str(root), settings=settings, load_from_yaml=load,
                           get=lambda key, default=None: settings.get(key, default))


def test_auto_preview_worker_forces_candidate_generate_only_and_blocks_queries(tmp_path, monkeypatch):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    config = fake_config(root)
    captured = {}
    main = ModuleType("Main")
    main.__file__ = str(root / "Main.py")
    main.GLOBAL_CONFIG = config
    def run():
        captured["argument"] = sys.argv[1]
        captured["test_mode"] = config.settings["test_mode"]
        captured["email"] = config.settings["use_email_send"]
        captured["s3"] = config.settings["use_s3_upload"]
        with pytest.raises(RuntimeError, match="ET query disabled"):
            main.etdata_query()
        with pytest.raises(RuntimeError, match="WIP query disabled"):
            main.wipdata_query()
        assert main.inlinedata_query().empty
        main._RUN = SimpleNamespace(data={'reports':['generated']})
        return 0
    main.main = run
    monkeypatch.setitem(sys.modules, "Main", main)
    assert rr._preview_worker("TEST", "auto", "LOT.1", "STEP-1") == 0
    assert captured['argument'] == "_TRIGGER_TEST_LOT.1_STEP-1"
    assert captured["test_mode"] is True
    assert captured["email"] is False and captured["s3"] is False
    assert __import__("os").environ["AUTO_REPORT_OPS_ROOT"] == str(root / "RUN" / "OPS")


def test_preview_worker_rejects_config_paths_outside_candidate(tmp_path, monkeypatch):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    main = ModuleType("Main")
    main.__file__ = str(root / "Main.py")
    main.GLOBAL_CONFIG = fake_config(root, tmp_path / "outside.xlsx")
    main._main_impl = lambda command: pytest.fail("must reject external path before Main")
    monkeypatch.setitem(sys.modules, "Main", main)
    with pytest.raises(ValueError, match="escapes the candidate"):
        rr._preview_worker("TEST", "auto", "LOT.1", "STEP-1")


def test_daily_preview_worker_freezes_send_false_and_candidate_db(tmp_path, monkeypatch):
    _, root = candidate(tmp_path, ["reformatter/report_items.yaml"])
    monkeypatch.setattr(rr, "_candidate_root", lambda: root)
    config = fake_config(root)
    main = ModuleType("Main")
    main.__file__ = str(root / "Main.py")
    main.GLOBAL_CONFIG = config
    captured = {}
    def report(request):
        captured.update(request)
        return {"status": "done"}
    main._daily_trend_report = report
    main._execute_serially = lambda action: action()
    scheduler = ModuleType("Scheduler")
    scheduler.load_config = lambda **kwargs: {"daily_trend": {"products": [], "recipients": ["ignored"], "enabled": True}}
    monkeypatch.setitem(sys.modules, "Main", main)
    monkeypatch.setitem(sys.modules, "Scheduler", scheduler)
    assert rr._preview_worker("TEST", "daily_trend", None, None) == 0
    assert captured["send"] is False
    assert captured["settings"]["recipients"] == []
    assert captured["settings"]["enabled"] is False
    assert captured["settings"]["ml_table_dir"] == str(root / "RUN" / "DB")


def test_prepare_accepts_explicit_input_directory_and_receipt_tracks_input_changes(tmp_path, monkeypatch):
    source, root = candidate(tmp_path, ['reformatter', 'RUN/DB'])
    review = make_receipt(root, monkeypatch)
    (root / 'RUN/DB/fixture.parquet').write_bytes(b'changed fixture')
    with pytest.raises(ValueError, match='stale'):
        rr._read_receipt(root, review)
    pass_check(root)
    review = rr.record(*map(str, artifacts(root)), 'TEST', 'new sample')
    (source / 'reformatter/report_items.yaml').write_text('source changed', encoding='utf-8')
    with pytest.raises(ValueError, match='drifted'):
        rr.promotion_plan(review)


def test_item_commands_preserve_other_service_and_options(tmp_path, monkeypatch):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, '__file__', str(root / 'report_review.py'))
    monkeypatch.setattr(rr, '_candidate_root', lambda: root)
    path = root / 'reformatter/TEST_reformatter.csv'
    path.parent.mkdir(exist_ok=True)
    path.write_text('ALIAS,REPORT ORDER\nA,1\nB,2\nOTHER,\n', encoding='utf-8')
    parse = rr.build_parser().parse_args
    result = rr.item_command(parse(['items-remove', '--vehicle', 'TEST', '--service', 'daily_trend', '--item', 'B']))
    assert set(result['daily_trend']) == {'A'}
    rr.item_command(parse(['items-add', '--vehicle', 'TEST', '--service', 'both', '--item', 'A', '--split-column', 'KNOB_ETCH']))
    result = rr.item_command(parse(['items-add', '--vehicle', 'TEST', '--service', 'daily_trend', '--item', 'A', '--time-column', 'ETCH']))
    assert result['daily_trend']['A'] == {'time_column':'ETCH', 'split_columns':['KNOB_ETCH']}
    result = rr.item_command(parse(['items-list', '--vehicle', 'TEST', '--service', 'mlmode']))
    assert result['mlmode']['A']['time_column'] == '' and 'B' in result['mlmode']
    with pytest.raises(ValueError, match='ineligible'):
        rr.item_command(parse(['items-add', '--vehicle', 'TEST', '--service', 'daily_trend', '--item', 'OTHER']))


@pytest.mark.parametrize('body', ['<html><img src="/file.png"></html>',
    '<html>' + '<img src="data:image/png;base64,AA==">'*10 + '</html>'])
def test_record_rejects_noninline_or_overflow_sample(tmp_path, monkeypatch, body):
    _, root = candidate(tmp_path)
    monkeypatch.setattr(rr, '_candidate_root', lambda: root)
    pass_check(root)
    html, ppt = artifacts(root)
    html.write_text(body, encoding='utf-8')
    with pytest.raises(ValueError):
        rr.record(str(html), str(ppt), 'TEST', 'bad sample')

