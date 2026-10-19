"""Ensure the offline preview and browser export describe the current App mapping."""
import base64
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def payload(fragment):
    encoded = re.search(r'<script type="application/octet-stream" data-payload>([^<]+)</script>', fragment)[1]
    return json.loads(gzip.decompress(base64.b64decode(encoded)))


@pytest.fixture(scope='module')
def preview():
    return payload((ROOT/'reports/six-direction-preview.html').read_text())


def test_preview_config_and_control_sources_are_current(preview):
    config_path = ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'
    config = json.loads(config_path.read_text())
    assert preview['config_sha256'] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert preview['installation'] == json.loads((ROOT/'public/tianji-installation.json').read_text())
    assert preview['installation']['installation_id'] == config['installation_id']
    required_sources = {str((config_path.parent / source['urdf']).resolve().relative_to(ROOT))
                        for source in config['tool_tcp'].values()}
    required_sources.add('public/tianji-model.json')
    assert required_sources <= preview['source_sha256'].keys()
    for field in ('teleop_translation_xy_sign', 'teleop_translation_z_sign'):
        assert preview[field] == config[field]
    for relative, fingerprint in preview['source_sha256'].items():
        assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() == fingerprint


def test_export_is_the_same_snapshot_as_inline_preview(preview):
    class Wrapper(HTMLParser):
        source = None

        def handle_starttag(self, tag, attrs):
            if tag == 'iframe':
                self.source = dict(attrs).get('srcdoc')

    wrapper = Wrapper()
    wrapper.feed((ROOT/'reports/six-direction-browser.html').read_text())
    assert wrapper.source
    assert payload(wrapper.source) == preview


@pytest.mark.parametrize('side,channel,vertical', [('left', 'A', 1), ('right', 'B', 1)])
def test_all_six_preview_targets_match_corrected_hardware_side(preview, side, channel, vertical):
    assert preview['installation']['arm_channels'][side] == channel
    assert preview['initial'] == [0.]*7
    expected = [[.02, 0, 0], [-.02, 0, 0], [0, .02, 0], [0, -.02, 0],
                [0, 0, .02*vertical], [0, 0, -.02*vertical]]
    for trace, delta in zip(preview['traces'][side], expected, strict=True):
        np.testing.assert_allclose(trace['delta'], np.array(delta)*preview['scale'], atol=1e-10)
        assert trace['frames'][0]['q'] == [0.]*7
        assert len(trace['frames']) == 31
        assert np.isfinite(trace['error_mm'])
