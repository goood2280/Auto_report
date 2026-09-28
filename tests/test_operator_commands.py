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


@pytest.mark.parametrize('identity', ['person.one', 'person.one@samsung.com'])
def test_person_overrides_default_groups_and_disabled_delivery(identity, monkeypatch):
    command = main._parse_command(['--send-user', identity, '--prime-key', 'vehicle_A_L001.1_S1', '--single'])
    assert command['argument'] == '_TRIGGER_SINGLE_vehicle_A_L001.1_S1'
    cfg = config(email_receiver=['HOL', 'OTHER'], use_email_send=False, DB_Setting_mode=True, report_making=False)
    recipient = main._apply_command_settings(command, cfg)
    assert cfg.settings['email_receiver'] == ['person.one@samsung.com']
    assert cfg.settings['use_email_send'] is True
    assert cfg.settings['report_making'] is True
    assert cfg.settings['DB_Setting_mode'] is False
    assert cfg.settings['use_s3_upload'] is False
    monkeypatch.setattr(main, 'GLOBAL_CONFIG', cfg)
    calls = []
    monkeypatch.setattr(main, '_durable_mail', lambda *args: calls.append(args) or 'sent')
    assert main._send_report_files('report.html', 'report.pptx', [recipient], 'title', 'id') == 'sent'
    assert [r['email'] for r in calls[0][1]] == ['person.one@samsung.com']


def test_department_exact_sheet_only(tmp_path, monkeypatch):
    command = main._parse_command(['--send-dept', 'PROCESS TEAM', '--prime-key', 'vehicle_A_L001.1_S1'])
    cfg = config(email_list_path=mailing_book(tmp_path), email_receiver=['HOL'])
    recipient = main._apply_command_settings(command, cfg)
    monkeypatch.setattr(main, 'GLOBAL_CONFIG', cfg)
    calls = []
    monkeypatch.setattr(main, '_durable_mail', lambda *args: calls.append(args) or 'sent')
    main._send_report_files('report.html', 'report.pptx', [recipient], 'title', 'id')
    assert {r['email'] for r in calls[0][1]} == {'person.one@samsung.com', 'person.two@samsung.com'}
    for sheet in ('MISSING', 'EMPTY', 'person@samsung.com'):
        with pytest.raises(ValueError):
            main._apply_command_settings(dict(command, recipient=sheet), cfg)


@pytest.mark.parametrize('args', [
    ['--send-user', 'one,two', '--prime-key', 'V_L_S'],
    ['--send-user', 'one;two', '--prime-key', 'V_L_S'],
    ['--send-user', ''], ['--send-dept', 'TEAM'],
    ['--init-db', 'V', '--single'], ['--single', 'V'],
    ['--init-db', 'V', '--send-user', 'one'],
    ['--init-db', ''], ['--init-db', '_TRIGGER_V_L_S'],
])
def test_invalid_commands_fail_before_processing(args):
    with pytest.raises(SystemExit):
        main._parse_command(args)


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
