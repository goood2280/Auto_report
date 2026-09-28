"""Offline score persistence and exact-prime-key trigger regression tests."""
import ast
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import Main as main
import My_Function as mf


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path / 'ops'))
    daily = tmp_path / 'TEST_daily'
    for date in ('2001-01-01', pd.Timestamp.now().date().isoformat()):
        folder = daily / f'date={date}'
        folder.mkdir(parents=True)
        rows = [dict(fab_lot_id=lot, root_lot_id='00001', step_id=step,
                     wafer_id=1, chip_x_pos=0, chip_y_pos=0, tkout_time=date,
                     item_id=item, et_value=value)
                for lot in ('00001.1', '00001.2')
                for step in ('001', '002')
                for item, value in [('I1', 2.0), ('RAMP', 4.0), ('unused', 99.0)]]
        pd.DataFrame(rows).to_parquet(folder / 'data.parquet', index=False)
    return daily


def formatter(vramp=False):
    rows = [dict(CATEGORY='REAL', ITEMID='I1', ALIAS='Current')]
    if vramp:
        rows.append(dict(CATEGORY='REAL', ITEMID='RAMP', ALIAS='Vramp'))
    return pd.DataFrame(rows)


@pytest.mark.parametrize('prefix', ['_TRIGGER_SINGLE_', 'TRIGGER_SINGLE_'])
def test_single_parser(prefix):
    assert main._parse_trigger(prefix + 'vehicle_A_00001.1_001') == (
        'SINGLE', 'vehicle_A', '00001.1', '001', None)


@pytest.mark.parametrize('value, expected', [
    ('vehicle_A', (None, 'vehicle_A', None, None, None)),
    ('_TRIGGER_vehicle_A_L_S', ('TRIGGER', 'vehicle_A', 'L', 'S', None)),
    ('_TRIGGER_NORMAL_vehicle_A_L_S', ('NORMAL', 'vehicle_A', 'L', 'S', None)),
])
def test_existing_parser(value, expected):
    assert main._parse_trigger(value) == expected


@pytest.mark.parametrize('vramp', [False, True])
def test_single_all_dates_exact_lot_and_step_cache_isolation(database, vramp):
    with duckdb.connect() as conn:
        regular = mf.load_daily_projected(conn, database, 1, formatter(vramp))
        single = mf.load_daily_projected(conn, database, None, formatter(vramp), lot='00001.1', step='001')
        assert set(single.fab_lot_id) == {'00001.1'}
        assert set(single.step_id) == {'001'}
        assert '2001-01-01' in set(single.loc[single.item_id.eq('I1'), 'tkout_time'])
        assert 'unused' not in set(single.item_id)
        assert '2001-01-01' not in set(regular.loc[regular.item_id.eq('I1'), 'tkout_time'])
        assert set(regular.fab_lot_id) == {'00001.1', '00001.2'}
        repeated = mf.load_daily_projected(conn, database, None, formatter(vramp), lot='00001.1', step='001')
        pd.testing.assert_frame_equal(single, repeated)
        other = mf.load_daily_projected(conn, database, None, formatter(vramp), lot='00001.2', step='002')
        assert set(other.fab_lot_id) == {'00001.2'}
        assert set(other.step_id) == {'002'}
        missing = mf.load_daily_projected(conn, database, None, formatter(vramp), lot="absent' OR 1=1 --", step='001')
        assert missing.empty


def board():
    return pd.DataFrame([[100, 33.333333], [np.nan, 0]],
                        index=pd.MultiIndex.from_tuples([('전기', 'I1'), ('Leakage', 'NA')]),
                        columns=pd.MultiIndex.from_tuples([('00001.1', 1), ('00001.2', 2)]))


def read_scores(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')


def test_score_matches_html_cells_and_preserves_identifiers(tmp_path):
    path = mf.save_score_csv(board(), tmp_path, 'vehicle_A', '00001.1', '001', 'report.html')
    assert Path(path) == tmp_path / 'Score' / 'vehicle_A_score.csv'
    assert Path(path).read_bytes().startswith(b'\xef\xbb\xbf')
    data = read_scores(path)
    assert data.score.tolist() == ['100.0', '33.3', '', '0.0']
    assert set(data.prime_key) == {'vehicle_A_00001.1_001'}
    assert set(data.dc_step_id) == {'001'}
    assert data.item_id.tolist() == ['I1', 'I1', 'NA', 'NA']
    assert set(data.fab_lot_id) == {'00001.1', '00001.2'}
    assert set(data.html_file) == {'report.html'}


def test_score_republish_replaces_snapshot_and_keeps_other_prime_keys(tmp_path):
    path = mf.save_score_csv(board(), tmp_path, 'V', '00001.1', '001', 'old.html')
    mf.save_score_csv(board(), tmp_path, 'V', '00001.1', '002', 'other.html')
    changed = board().iloc[:1, :1].copy()
    changed.iloc[0, 0] = 80
    for _ in range(2):
        mf.save_score_csv(changed, tmp_path, 'V', '00001.1', '001', 'new.html')
    data = read_scores(path)
    assert len(data) == 5
    assert data.loc[data.dc_step_id.eq('001'), 'score'].tolist() == ['80.0']
    assert set(data.loc[data.dc_step_id.eq('001'), 'html_file']) == {'new.html'}
    assert len(data.loc[data.dc_step_id.eq('002')]) == 4


def test_score_failed_replace_preserves_original(tmp_path, monkeypatch):
    path = mf.save_score_csv(board(), tmp_path, 'V', 'L', 'S', 'old.html')
    before = Path(path).read_bytes()
    def fail(*args):
        raise PermissionError('simulated file open in Excel')
    monkeypatch.setattr(mf.os, 'replace', fail)
    with pytest.raises(PermissionError):
        mf.save_score_csv(board().iloc[:1], tmp_path, 'V', 'L', 'S', 'new.html')
    assert Path(path).read_bytes() == before
    assert not list(Path(path).parent.glob('._writing_*'))


def test_score_lock_prevents_lost_update(tmp_path):
    path = mf.save_score_csv(board(), tmp_path, 'V', 'L', 'S', 'old.html')
    before = Path(path).read_bytes()
    with mf.process_lock(path + '.lock'):
        with pytest.raises(RuntimeError):
            mf.save_score_csv(board(), tmp_path, 'V', 'L', 'T', 'new.html')
    assert Path(path).read_bytes() == before


def test_single_pipeline_loads_all_dates_and_skips_comparison_products(database):
    # Execute the production loading/merge branches with a spy for expensive pivot/rendering.
    tree = ast.parse(Path(main.__file__).read_text(encoding='utf-8'))
    impl = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_main_impl')
    branches = [n for n in ast.walk(impl) if isinstance(n, ast.If)]
    single = next(n for n in branches if ast.unparse(n.test) == "trigger_mode == 'SINGLE'")
    comparison = next(n for n in branches if ast.unparse(n.test) == "trigger_mode != 'SINGLE' and vehicle not in with_vehicle")
    with duckdb.connect() as conn:
        scope = dict(trigger_mode='SINGLE', vehicle='TEST', trigger_lot='00001.1', trigger_step='001',
                     DB_et_daily=database, viewing_period=1, reformatter=formatter(), conn=conn,
                     with_vehicle=['COMPARISON'], load_daily_projected=mf.load_daily_projected)
        exec(compile(ast.Module(body=[single, comparison], type_ignores=[]), '<pipeline>', 'exec'), scope)
    assert set(scope['raw_df'].fab_lot_id) == {'00001.1'}
    assert len(scope['raw_df']) == 2
    assert scope['viewing_period'] == 1
