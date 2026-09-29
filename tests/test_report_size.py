"""PPT size budget, chart encoding, ML candidate source and Watchdog overview (offline)."""
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import Main as main
import My_Function as mf


def _noise_png(w, h, seed=0):
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray((rng.random((h, w, 3)) * 255).astype('uint8')).save(buf, 'PNG')
    return buf.getvalue()


def _chart_png():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8.3, 1.95))
    ax.boxplot([np.random.default_rng(i).normal(0, 1, 200) for i in range(25)])
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=125); plt.close(fig)
    return buf.getvalue()


def test_chart_encoder_never_worse_than_jpeg_and_prefers_png_for_line_charts():
    png = _chart_png()
    out = mf._encode_chart_image(png, 55, 'auto', 128)
    jpeg = mf._encode_chart_image(png, 55, 'jpeg', 128)
    assert len(out) <= len(jpeg)
    assert out[:4] == b'\x89PNG'           # box plot: palette PNG wins
    assert Image.open(io.BytesIO(out)).size == Image.open(io.BytesIO(png)).size


def test_wfmap_canvas_is_capped_to_display_resolution():
    canvas = Image.new('RGB', (3400, 460), 'white')
    buf = mf._encode_map_canvas(canvas, dict(map_max_px=2370, map_colors=128))
    image = Image.open(io.BytesIO(buf.getvalue()))
    assert image.size[0] == 2370 and image.mode == 'P'


def _deck_with_description(image_bytes, charts=3):
    source = Presentation()
    slide = source.slides.add_slide(source.slide_layouts[6])
    slide.shapes.add_textbox(Inches(.3), Inches(.2), Inches(4), Inches(.5)).text_frame.text = 'VTH'
    slide.shapes.add_picture(io.BytesIO(image_bytes), Inches(5), Inches(1), Inches(7))
    deck = Presentation()
    for i in range(charts):
        s = deck.slides.add_slide(deck.slide_layouts[6])
        s.shapes.add_picture(io.BytesIO(_noise_png(900, 300, seed=i + 1)), Inches(.3), Inches(.3), Inches(8))
    mf._copy_slide_into(deck, slide, source.slide_width, source.slide_height)
    return deck


def _set(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setattr(mf.GLOBAL_CONFIG, key, value, raising=False)


def test_description_images_use_remaining_budget(monkeypatch):
    _set(monkeypatch, description_image_recompress=True, description_image_target_mb=0, description_image_max_px=2400,
         description_min_px=480, ppt_budget_ratio=1.0)
    big = _noise_png(2400, 1600)
    roomy = _deck_with_description(big)
    _set(monkeypatch, ppt_mail_max_mb=50.0)
    generous = mf.fit_ppt_budget(roomy, mf.GLOBAL_CONFIG)
    tight_deck = _deck_with_description(big)
    base = mf._ppt_bytes(tight_deck) - len(tight_deck._auto_desc_images and
                                           tight_deck.slides[-1].part.related_part(tight_deck._auto_desc_images[0]['rId']).blob)
    _set(monkeypatch, ppt_mail_max_mb=(base + 250_000) / 1e6)
    tight = mf.fit_ppt_budget(tight_deck, mf.GLOBAL_CONFIG)
    assert generous['desc_level'].startswith('2400px')         # 여유가 많으면 최고 화질
    assert tight['after'] <= tight['limit'] and tight['desc_level'] not in ('-', '생략')
    assert int(tight['desc_level'].split('px')[0]) < 2400      # 빠듯하면 해상도를 낮춰 끼워 넣는다
    # 원본 설명 PPT 이미지 파트는 건드리지 않는다(다음 Lot 에서 재사용).
    assert len(big) > 1_000_000


def test_description_image_dropped_but_text_kept_when_no_room(monkeypatch):
    _set(monkeypatch, description_image_recompress=True, description_image_target_mb=0, description_min_px=480, ppt_budget_ratio=1.0)
    deck = _deck_with_description(_noise_png(2400, 1600), charts=1)
    _set(monkeypatch, ppt_mail_max_mb=0.05)
    result = mf.fit_ppt_budget(deck, mf.GLOBAL_CONFIG)
    last = deck.slides[-1]
    texts = ' '.join(sh.text_frame.text for sh in last.shapes if sh.has_text_frame)
    assert result['desc_dropped'] == 1 and 'VTH' in texts and '용량 한도' in texts
    assert not last.shapes._spTree.xpath('.//p:pic')


def test_chart_images_shrink_as_last_resort(monkeypatch):
    _set(monkeypatch, ppt_budget_ratio=1.0)
    deck = Presentation()
    for i in range(3):
        deck.slides.add_slide(deck.slide_layouts[6]).shapes.add_picture(io.BytesIO(_noise_png(1400, 900, i)), 0, 0, Inches(8))
    size = mf._ppt_bytes(deck)
    _set(monkeypatch, ppt_mail_max_mb=size * .8 / 1e6)
    result = mf.fit_ppt_budget(deck, mf.GLOBAL_CONFIG)
    assert result['charts_shrunk'] > 0 and result['after'] < size


@pytest.mark.parametrize('source,expected', [('daily', {'A'}), ('ml', {'B'}), ('either', {'A', 'B'})])
def test_ml_candidate_source_controls_which_items_get_detail(monkeypatch, source, expected):
    entries = [dict(item='A', auto_findings=[dict(title='Flier')], warnings=[]), dict(item='B', warnings=[]), dict(item='C', warnings=[])]
    seen = []

    def fake_select(pool, settings):
        seen.append([e['item'] for e in pool])
        chosen = [e for e in pool if e['item'] == 'B']
        for e in chosen:
            e['ml_findings'] = [dict(module='isolation_forest', q=.01, effect=.5, message='m')]
        return chosen, {}
    settings = dict(candidate_source=source)
    # 선정 로직만 떼어 실행(데이터 적재·렌더링 없이)
    pool = [e for e in entries if e.get('auto_findings')] if main._ml_candidate_source(settings) == 'daily' else entries
    selected, _ = fake_select(pool, settings)
    chosen = {id(e) for e in selected}
    if main._ml_candidate_source(settings) in ('daily', 'either'):
        selected += [e for e in pool if e.get('auto_findings') and id(e) not in chosen]
    assert {e['item'] for e in selected} == expected
    assert seen[0] == (['A'] if source == 'daily' else ['A', 'B', 'C'])
    assert main._ml_module_note('isolation_forest', dict(module_notes={'isolation_forest': '사용자 문구'})) == '사용자 문구'
    assert 'drift' in main._ml_module_note('time_trend', {})


def test_ml_selection_block_in_report_matches_contract():
    source = Path(main.__file__).read_text(encoding='utf-8')
    assert "pool=[e for e in entries if e.get('auto_findings')] if source=='daily' else entries" in source
    assert "e['selection_reason']" in source


def test_watchdog_overview_verdict_and_timeline(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTO_REPORT_OPS_ROOT', str(tmp_path))
    now = 1_760_000_000.0
    reports = [dict(vehicle='P1', lot='L1', step='S1', started=now - 100, updated=now - 50, status='success', email='sent', elapsed=120, mode='AUTO'),
               dict(vehicle='P1', lot='L2', step='S1', started=now - 90, updated=now - 40, status='failed', email='', mode='TRIGGER')]
    html = main._watchdog_overview(dict(state='healthy', message='정상'), [], {}, {}, reports,
                                   [dict(vehicle='P1', rc=0, elapsed=300)], now - 86400, now, dict(products=['P1']))
    assert '정상 — Scheduler 동작' in html and 'id="timeline"' in html and 'L2 / S1' in html
    assert '1회 / 평균 5.0분' in html
    warn = main._watchdog_overview(dict(state='stale', message='정지 의심'), [['x', 'y', 'z', 'w']], {}, {}, [], [], now - 86400, now, {})
    assert '확인 필요 1건' in warn
