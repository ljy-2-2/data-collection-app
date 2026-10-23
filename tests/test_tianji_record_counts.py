"""Episode count comparison must exclude preview without concealing dropped records."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'fusion_data_collection'))
from FusionDataCollector.collector import record_count_check
from FusionDataCollector.collector import camera_queue_quality


@pytest.mark.parametrize('last,affected', [(800,False), (1099,False), (1100,True), (1500,True)])
def test_camera_overflow_distinguishes_preview_from_recording_with_clock_uncertainty(last, affected):
    start = dict(adopted_preview_stream=True, episode_subscription_pc_monotonic_ns=1000)
    clock = dict(offset_ns=200, uncertainty_ns=100)
    final = dict(camera_queue_overflow={'usb-2':dict(dropped=3, last_timestamp_ns=last)})
    result = camera_queue_quality(start, final, clock)
    assert bool(result['possibly_affected_episode_cameras']) is affected
    assert result['stream_totals'] == final['camera_queue_overflow']
    assert result['counter_scope'] == 'whole_stream_including_preview'


def test_camera_overflow_without_boundary_is_flagged_conservatively():
    final = dict(camera_queue_overflow={'usb-4':dict(dropped=1, last_timestamp_ns=900)})
    assert camera_queue_quality({}, final, {})['possibly_affected_episode_cameras'] == ['usb-4']
    assert not camera_queue_quality({}, {}, {})['possibly_affected_episode_cameras']


@pytest.mark.parametrize('bad', ['none','image_missing','joint_missing','image_extra','baseline_missing',
                                 'baseline_negative','baseline_exceeds','baseline_float','server_missing'])
def test_adopted_stream_uses_recording_boundary_and_still_detects_count_errors(bad):
    start = dict(adopted_preview_stream=True,episode_count_baseline=dict(
        frames={'usb-1':601,'usb-2':590,'usb-4':583},joint_samples=2002))
    final = dict(frames=[1004,992,984],joint_samples=3347)
    disk = {'usb-1':403,'usb-2':402,'usb-4':401}
    joints=1345
    if bad == 'image_missing':disk['usb-1']-=1
    if bad == 'joint_missing':joints-=1
    if bad == 'image_extra':disk['usb-1']+=1
    if bad == 'baseline_missing':del start['episode_count_baseline']
    if bad == 'baseline_negative':start['episode_count_baseline']['joint_samples']=-1
    if bad == 'baseline_exceeds':start['episode_count_baseline']['joint_samples']=9999
    if bad == 'baseline_float':start['episode_count_baseline']['joint_samples']=2002.0
    if bad == 'server_missing':del final['frames']
    before=deepcopy(final)
    result=record_count_check(start, final, disk, joints)
    assert result['matches'] is (bad == 'none')
    assert final == before  # Preserve the server's original whole-stream totals.
    if bad == 'none':
        assert result['expected'] == result['stored']
        assert result['server_total']['joint_samples'] == 3347


def test_legacy_whole_stream_keeps_strict_counts():
    final=dict(frames=[4,5,6],joint_samples=10)
    assert record_count_check({}, final, {'usb-1':4,'usb-2':5,'usb-4':6},10)['matches']
    assert not record_count_check({}, final, {'usb-1':3,'usb-2':5,'usb-4':6},10)['matches']
