"""ML 기준값 정렬 계약: 허용 키·범위만, My_config.py 의 해당 숫자만 바꾸고, 재판정이 원래 판정과 같아야 한다."""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import My_Function as mf
import Manager as manager_module
import ml_threshold_tuner as tuner
from test_ml_insight import entry


class FakeLLM:
    def __init__(self, data=None, ok=True):
        self.data, self.ok, self.prompts = data, ok, []

    def available(self):
        return True

    def complete_json(self, prompt, system=None):
        self.prompts.append(prompt)
        return dict(ok=self.ok, data=self.data, error='' if self.ok else 'HTTP 500')


def install(tmp_path, effects=('r2', 'tail', 'knob', 'none')):
    """임시 설치본: My_config.py 사본 + 인자 스크리닝 결과(influence.json, Main 감사 파일과 같은 형식)."""
    shutil.copy(ROOT / 'My_config.py', tmp_path / 'My_config.py')
    entries = [entry(effect, seed) for seed, effect in enumerate(effects)]
    for i, e in enumerate(entries):
        e['item'] = f'ITEM{i}'
    mf.ml_factor_screen(entries, {})
    audit = [dict(vehicle=e['vehicle'], item=e['item'], step=e['step'],
                  factors=[{k: v for k, v in r.items() if k != 'plot'} for r in e['_factors']['rows']]) for e in entries]
    run = tmp_path / 'RUN' / 'OPS' / 'mlmode' / 'run1'
    run.mkdir(parents=True)
    (run / 'influence.json').write_text(json.dumps(audit, ensure_ascii=False, default=str), encoding='utf-8')
    return entries


def test_current_values_come_from_my_config_mlmode():
    values = tuner.current_values(str(ROOT / 'My_config.py'))
    assert set(values) == set(tuner.TUNABLE)
    import My_config
    for key, value in values.items():
        assert My_config.GLOBAL_CONFIG.mlmode[key] == pytest.approx(value)


def test_resimulation_with_current_values_matches_screen(tmp_path):
    entries = install(tmp_path)
    evidence = tuner.latest_evidence(str(tmp_path / 'RUN' / 'OPS'))
    current = tuner.current_values(str(tmp_path / 'My_config.py'))
    expected = {(f"T/{e['item']}/S1", r['column'], s) for e in entries for r in e['_factors']['rows'] for s in r['signals']}
    assert expected and set(tuner.simulate(evidence['rows'], current)['hits']) == expected
    strict = tuner.simulate(evidence['rows'], tuner._scaled(current, 2))['signals']
    loose = tuner.simulate(evidence['rows'], tuner._scaled(current, -2))['signals']
    assert strict <= len(expected) <= loose


def test_ai_values_are_filtered_to_allowed_keys_and_ranges(tmp_path):
    install(tmp_path)
    llm = FakeLLM(dict(changes={'factor_r2_min': .5, 'factor_tail_min': 99, 'viewing_period': 1, 'factor_fdr_alpha': .02},
                       reason='확실한 것만'))
    proposal = tuner.propose('R² 신호가 너무 많이 잡혀', llm, str(tmp_path / 'My_config.py'), str(tmp_path / 'RUN' / 'OPS'))
    assert proposal['source'] == 'ai' and proposal['values'] == {'factor_r2_min': .5, 'factor_fdr_alpha': .02}
    assert any('factor_tail_min' in n for n in proposal['notes']) and any('viewing_period' in n for n in proposal['notes'])
    assert 'what_if' in llm.prompts[0] and 'factor_r2_min' in llm.prompts[0]


def test_rule_fallback_when_ai_fails_and_no_direction_means_no_change(tmp_path):
    install(tmp_path)
    config, ops = str(tmp_path / 'My_config.py'), str(tmp_path / 'RUN' / 'OPS')
    strict = tuner.propose('밑둥 들림이 너무 많이 잡혀. 확실한 것만', FakeLLM(ok=False), config, ops)
    assert strict['source'] == 'rule' and strict['ai']['error'] == 'HTTP 500'
    assert strict['values']['factor_tail_min'] > strict['current']['factor_tail_min']
    assert strict['after']['signals'] <= strict['before']['signals']
    loose = tuner.propose('놓치는 게 많아 완화해줘', None, config, ops)
    assert loose['values']['factor_r2_min'] < loose['current']['factor_r2_min']
    assert tuner.propose('그냥 봐줘', None, config, ops)['values'] == {}
    mixed = tuner.propose('밑둥 들림은 확실한 것만 보고 싶고, R² 는 0.2 정도 약한 상관도 보고 싶어', None, config, ops)
    assert mixed['values']['factor_r2_min'] == 0.2 and mixed['values']['factor_tail_min'] > mixed['current']['factor_tail_min']
    assert 'factor_level_effect_min' not in mixed['values']   # 말하지 않은 신호는 그대로
    assert tuner.rule_changes('하단 꼬리 25% 정도 더 잡혀도 돼 완화', mixed['current'])[0]['factor_tail_min'] < mixed['current']['factor_tail_min']


def test_apply_changes_only_those_numbers_backs_up_and_detects_edits(tmp_path):
    install(tmp_path)
    config, ops = str(tmp_path / 'My_config.py'), str(tmp_path / 'RUN' / 'OPS')
    before = (tmp_path / 'My_config.py').read_bytes()
    proposal = tuner.propose('엄격하게', None, config, ops)
    result = tuner.apply(proposal, config, ops)
    after = (tmp_path / 'My_config.py').read_bytes()
    assert tuner.current_values(config) == dict(proposal['current'], **proposal['values'])
    # 바뀐 바이트는 기준값 숫자뿐 — 나머지 줄은 그대로(줄끝 포함)
    changed = [(a, b) for a, b in zip(before.split(b'\n'), after.split(b'\n')) if a != b]
    assert len(before.split(b'\n')) == len(after.split(b'\n')) and changed
    assert all(b'factor_' in a for a, _ in changed)
    assert Path(result['backup']).read_bytes() == before
    assert tuner.history(ops)[0]['after'] == proposal['values']
    with pytest.raises(ValueError, match='바뀌었습니다'):
        tuner.apply(proposal, config, ops)


def test_manager_threshold_endpoints_require_confirmation(tmp_path):
    install(tmp_path)
    manager = manager_module.Manager(tmp_path, lambda: {})
    manager.llm.available = lambda: False
    info = manager.thresholds()
    assert info['evidence']['flagged'] >= 1 and len(info['values']) == len(tuner.TUNABLE)
    proposal = manager.threshold_propose({'intent': '더 엄격하게'})
    with pytest.raises(ValueError):
        manager.threshold_apply({'proposal_id': proposal['proposal_id']})
    result = manager.threshold_apply({'proposal_id': proposal['proposal_id'], 'confirmed': True})
    assert '반영' in result['message']
    with pytest.raises(ValueError, match='이미 적용'):
        manager.threshold_apply({'proposal_id': proposal['proposal_id'], 'confirmed': True})
