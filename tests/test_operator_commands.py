"""Operator commands never use live mail or corporate data in these tests."""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import Main as main
import My_Function as mf
from My_config import Config


def config(**settings):
    obj = SimpleNamespace(settings=settings)
    obj.get = lambda key, default=None: obj.settings.get(key, default)
    return obj


def mailing_book(tmp_path):
    path = tmp_path / 'mail.xlsx'
    with pd.ExcelWriter(path) as book:
        pd.DataFrame({'KNOX_ID': [' person.one ', 'person.two@samsung.com', None, ' ']}).to_excel(book, sheet_name='PROCESS TEAM', index=False)
        pd.DataFrame({'KNOX_ID': ['wrong.recipient']}).to_excel(book, sheet_name='HOL', index=False)
        pd.DataFrame({'KNOX_ID': []}).to_excel(book, sheet_name='EMPTY', index=False)
    return path


def test_default_mail_domain_and_existing_address(tmp_path):
    path = mailing_book(tmp_path)
    assert [r['email'] for r in mf.get_email_list(path, 'PROCESS TEAM')] == [
        'person.one@samsung.com', 'person.two@samsung.com']
    assert mf.get_email_list(path, 'HOL', domain='example.test')[0]['email'] == 'wrong.recipient@example.test'


@pytest.mark.parametrize('identity', ['person.one', 'person_two', 'person-three'])
def test_person_overrides_default_groups_and_disabled_delivery(identity, monkeypatch):
    def forbid_excel(*args, **kwargs):
        pytest.fail('개인 발송에서 메일링 엑셀을 읽으면 안 됩니다')
    monkeypatch.setattr(pd, 'ExcelFile', forbid_excel)
    command = main._parse_command(['--send-user', identity, '--prime-key', 'vehicle_A_L001.1_S1', '--single'])
    assert command['argument'] == '_TRIGGER_SINGLE_vehicle_A_L001.1_S1'
    expected = identity + '@samsung.com'
    cfg = config(email_receiver=['HOL', 'OTHER'], use_email_send=False, DB_Setting_mode=True, report_making=False)
    recipient = main._apply_command_settings(command, cfg)
    assert cfg.settings['email_receiver'] == [expected]
    assert cfg.settings['use_email_send'] is True
    assert cfg.settings['report_making'] is True
    assert cfg.settings['DB_Setting_mode'] is False
    assert cfg.settings['use_s3_upload'] is False
    monkeypatch.setattr(main, 'GLOBAL_CONFIG', cfg)
    calls = []
    monkeypatch.setattr(main, '_durable_mail', lambda *args: calls.append(args) or 'sent')
    assert main._send_report_files('report.html', 'report.pptx', [recipient], 'title', 'id') == 'sent'
    assert [r['email'] for r in calls[0][1]] == [expected]


def test_default_delivery_reads_configured_excel_sheet(tmp_path, monkeypatch):
    command = main._parse_command(['vehicle_A'])
    cfg = config(email_list_path=mailing_book(tmp_path), email_receiver=['PROCESS TEAM'], use_email_send=True)
    assert main._apply_command_settings(command, cfg) is None
    monkeypatch.setattr(main, 'GLOBAL_CONFIG', cfg)
    calls = []
    monkeypatch.setattr(main, '_durable_mail', lambda *args: calls.append(args) or 'sent')
    main._send_report_files('report.html', 'report.pptx', cfg.get('email_receiver'), 'title', 'id')
    assert {r['email'] for r in calls[0][1]} == {'person.one@samsung.com', 'person.two@samsung.com'}
    for sheet in ('MISSING', 'EMPTY'):
        with pytest.raises(ValueError):
            main._trigger_receivers(cfg.get('email_list_path'), sheet)


@pytest.mark.parametrize('args', [
    ['--send-user', 'one,two', '--prime-key', 'V_L_S'],
    ['--send-user', 'one;two', '--prime-key', 'V_L_S'],
    ['--send-user', ''], ['--send-dept', 'TEAM'],
    ['--init-db', 'V', '--single'], ['--single', 'V'],
    ['--init-db', 'V', '--send-user', 'one'],
    ['--init-db', ''], ['--init-db', '_TRIGGER_V_L_S'],
    ['--send-dept', 'PROCESS TEAM', '--prime-key', 'V_L_S'],
    ['--send-user', 'person.one@samsung.com', '--prime-key', 'V_L_S'],
    ['--send-user', 'person one', '--prime-key', 'V_L_S'],
    ['--send-user', 'person@example.test', '--prime-key', 'V_L_S'],
    ['--send-user', 'a@samsung.com,b@samsung.com', '--prime-key', 'V_L_S'],
])
def test_invalid_commands_fail_before_processing(args):
    with pytest.raises(SystemExit):
        main._parse_command(args)


def test_config_lookup_preserves_priority_and_explicit_false_values():
    cfg = Config.__new__(Config)
    cfg.settings = {'key': None}
    cfg.generated_vars = {'key': False}
    cfg.env = {'key': 'environment'}
    cfg.key = 'code-default'
    assert cfg.get('key', 'fallback') is None
    cfg.settings.clear()
    assert cfg.get('key') is False
    cfg.generated_vars.clear()
    assert cfg.get('key') == 'environment'
    cfg.env.clear()
    assert cfg.get('key') == 'code-default'
    assert cfg.get('missing', 'fallback') == 'fallback'


def test_config_paths_keep_product_scope_and_trailing_separator(tmp_path):
    import os
    cfg = Config.__new__(Config)
    cfg.base_path = str(tmp_path)
    cfg.settings = dict(vehicle='vehicle_A', prod='PRODUCT', YOUR_PROJECT='TEST', KNOXID='test.user')
    cfg.generated_vars = {}
    cfg._generate_dependent_vars()
    paths = cfg.generated_vars
    assert paths['DB_et_daily'] == str(tmp_path / 'RUN' / 'DB' / 'vehicle_A_daily') + os.sep
    assert paths['html_save_path'] == str(tmp_path / 'RUN' / 'Report' / 'vehicle_A' / 'HTML') + os.sep
    assert paths['query_log'] == paths['loop_log'] == paths['error_log'] == str(tmp_path / 'RUN' / 'log' / 'PRODUCT_log.txt')
    assert paths['Final_et_log_path'] == str(tmp_path / 'RUN' / 'log' / 'vehicle_A_et_log_Final.csv')


@pytest.mark.parametrize('mode', ['TRIGGER', 'FORCE', 'NORMAL', 'ALL', 'SINGLE'])
def test_trigger_parser_modes(tmp_path, monkeypatch, mode):
    """All supported trigger modes recover the same vehicle/lot/step tuple."""
    monkeypatch.chdir(tmp_path)
    if mode in ('FORCE', 'ALL'):
        (tmp_path / 'reformatter').mkdir()
        # The longer name must win when one vehicle name is a suffix of another.
        for vehicle in ('A', 'vehicle_A'):
            (tmp_path / 'reformatter' / f'{vehicle}_reformatter.csv').touch()
        value = f'_TRIGGER_{mode}_OPS TEAM_vehicle_A_00001.1_001'
        expected_mail = 'OPS TEAM'
    else:
        mode_part = '' if mode == 'TRIGGER' else f'{mode}_'
        value = f'_TRIGGER_{mode_part}vehicle_A_00001.1_001'
        expected_mail = None

    assert main._parse_trigger(value) == (mode, 'vehicle_A', '00001.1', '001', expected_mail)


@pytest.mark.parametrize('value', [
    '_TRIGGER_FORCE_vehicle_A_L_S',
    '_TRIGGER_ALL__vehicle_A_L_S',
    '_TRIGGER_FORCE_OPS\nTEAM_vehicle_A_L_S',
])
def test_force_and_all_require_a_valid_mail_prefix_and_vehicle(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'reformatter').mkdir()
    (tmp_path / 'reformatter' / 'vehicle_A_reformatter.csv').touch()
    with pytest.raises(ValueError):
        main._parse_trigger(value)


def test_init_db_command_uses_200_day_full_refresh_without_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = dict(vehicle='TEST', SplitTimeSpan=3, QueryTimeSpan=7, now_minus=2,
                    test_mode=True, report_making=True, use_email_send=True, use_s3_upload=True)
    cfg = config(**settings)
    command = main._parse_command(['--init-db', 'TEST'])

    assert command == dict(argument='TEST', kind='init_db', recipient=None)
    assert main._apply_command_settings(command, cfg) is None
    assert cfg.settings == dict(vehicle='TEST', SplitTimeSpan=3, QueryTimeSpan=200, now_minus=0,
                                test_mode=False, report_making=False, use_email_send=False,
                                use_s3_upload=False, DB_Setting_mode=True, et_force_full_refresh=True)


def test_init_forces_200_days_despite_previous_incremental_refresh(tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 28, 12)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    monkeypatch.setattr(mf, 'datetime', FrozenDatetime)
    (tmp_path / 'reformatter').mkdir()
    pd.DataFrame([dict(CATEGORY='REAL', ITEMID='I', ALIAS='I', ABSOLUTE=True,
                       **{'SCALE FACTOR': 1, 'ADDP FORM': ''})]).to_csv('reformatter/TEST_reformatter.csv', index=False)
    settings = dict(vehicle='TEST', DB_et_daily=str(tmp_path / 'daily'), et_log_path=str(tmp_path / 'et_log.csv'),
                    SplitTimeSpan=7, QueryTimeSpan=2, now_minus=4, test_mode=True, report_making=True,
                    use_email_send=True, DB_Setting_mode=False)
    monkeypatch.setattr(mf.GLOBAL_CONFIG, 'settings', settings)
    main._apply_command_settings(main._parse_command(['--init-db', 'TEST']), mf.GLOBAL_CONFIG)
    assert settings['DB_Setting_mode'] and not settings['report_making'] and not settings['test_mode']
    assert not settings['use_email_send'] and not settings['use_s3_upload']
    requests = []
    def query(params, **kwargs):
        requests.append((params['dateFrom'], params['dateTo']))
        return pd.DataFrame([dict(tkout_time=date, et_value=1., temperature=25, lot_id='00001_1',
                                 fab_lot_id='00001.1', step_id='S1', wafer_id=1, total_site_cnt=1,
                                 step_seq='P1', item_id='I')
                             for date in pd.date_range(params['dateFrom'], params['dateTo'])])
    monkeypatch.setattr(mf, 'getData_with_retry', query)
    for _ in range(2):
        requests.clear()
        mf.etdata_query()
        dates = [d for start, end in requests for d in pd.date_range(start, end)]
        assert len(dates) == len(set(dates)) == 200
        assert min(dates) == pd.Timestamp('2026-03-13')
        assert max(dates) == pd.Timestamp('2026-09-28')
    assert len(list((tmp_path / 'daily').glob('date=*/data.parquet'))) == 200
    assert pd.read_csv(tmp_path / 'et_log.csv').prime_key.tolist() == ['TEST_00001.1_S1']


def test_force_extends_history_to_two_days_before_target_measurement():
    log = pd.DataFrame([
        {'lot_id': 'ABC12.1', 'dc_step_id': 'CC01', 'tkout_time': '2026-09-01 08:00:00'},
        {'lot_id': 'ABC12.1', 'dc_step_id': 'CC02', 'tkout_time': '2026-09-26 08:00:00'},
    ])
    assert main._force_viewing_period(log, 'ABC12.1', 'CC01', 7, datetime(2026, 9, 28)) == 29
    assert main._force_viewing_period(log, 'ABC12.1', 'CC02', 7, datetime(2026, 9, 28)) == 7
    with pytest.raises(ValueError, match='prime key'):
        main._force_viewing_period(log, 'ABC12.2', 'CC01', 7, datetime(2026, 9, 28))


def test_normal_uses_only_selected_13pt_shot_coordinates():
    shots = pd.DataFrame([
        dict(mask='P123', chip_x_pos=1, chip_y_pos=2, flat_zone='N', item_id='I1'),
        dict(mask='P123', chip_x_pos=3, chip_y_pos=4, flat_zone='N', item_id='I1'),
    ])
    zones = pd.DataFrame([
        dict(MASK='P123', CHIP_X_POS=1, CHIP_Y_POS=2, FLAT_ZONE_POS='N', **{'13pt':'o'}),
        dict(MASK='P123', CHIP_X_POS=3, CHIP_Y_POS=4, FLAT_ZONE_POS='N', **{'13pt':''}),
    ])
    selected = main._filter_normal_shots(shots, zones)
    assert selected[['chip_x_pos','chip_y_pos']].values.tolist() == [[1,2]]
    with pytest.raises(ValueError, match='13pt'):
        main._filter_normal_shots(shots, zones.drop(columns='13pt'))


def test_all_mode_keeps_retryable_mail_result_as_failure(tmp_path, monkeypatch):
    cfg = config(html_save_path=str(tmp_path), low_qual_ppt_save_path=str(tmp_path))
    monkeypatch.setattr(main, 'GLOBAL_CONFIG', cfg)
    monkeypatch.setattr(main, '_RUN', None)
    monkeypatch.setattr(main, 'insert_plots', lambda *a, **k: (None, None, {'I1':'chart'}))
    monkeypatch.setattr(main, '_trend_artifacts', lambda *a: ('<p>synthetic</p>', b'PPTX'))
    sent = []
    monkeypatch.setattr(main, '_send_report_files',
                        lambda *args: sent.append(args) or 'retryable')
    formatter = pd.DataFrame([{'CAT2':'Electrical','ALIAS':'I1'}])
    with pytest.raises(RuntimeError, match='retryable'):
        main._publish_all_trends(pd.DataFrame(), formatter, 'P123', 'ABC12.1',
                                 'ABC12', 'MFDC', 'CC01', 'POWER USER', '20260928')
    assert len(sent) == 1 and sent[0][2] == ['POWER USER']
    assert list(tmp_path.glob('*-ALL-Trends.html'))
