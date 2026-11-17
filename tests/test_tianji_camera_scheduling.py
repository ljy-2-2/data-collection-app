"""Camera-only thread masks, failure rollback and calling-thread isolation."""
import importlib.util
import os
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'fusion_camera_stream/controller'))
spec = importlib.util.spec_from_file_location('camera_scheduling_under_test',
    ROOT / 'fusion_data_collection/FusionDataCollector/controller_v22/streaming.py')
streaming = importlib.util.module_from_spec(spec)
spec.loader.exec_module(streaming)


@pytest.mark.parametrize('failure', [None, 'restricted', 'syscall'])
def test_sender_mask_shares_non_realtime_cpus_and_rolls_back(monkeypatch, failure):
    original = (8,) + (0,)*127
    target = (13,) + (0,)*127
    state = [original]
    writes = []
    def affinity(mask=None):
        if mask is None:
            return state[0]
        writes.append(mask)
        if mask == target and failure == 'syscall':
            raise OSError('affinity denied')
        state[0] = (1,) + (0,)*127 if mask == target and failure == 'restricted' else mask
    monkeypatch.setattr(streaming, 'thread_affinity', affinity)
    if failure:
        with pytest.raises((OSError, RuntimeError)):
            streaming.move_stream_sender()
        assert state[0] == original
        assert writes == [target, original]
    else:
        assert streaming.move_stream_sender() == original
        assert state[0] == target
        streaming.thread_affinity(original)
        assert state[0] == original


def test_libc_affinity_changes_only_the_calling_thread_and_restores():
    main_mask = streaming.thread_affinity()
    allowed = os.sched_getaffinity(0)
    selected = min(allowed)
    mask = [0]*128
    mask[selected//8] = 1 << (selected%8)
    errors = []
    entered, release = threading.Event(), threading.Event()
    def worker():
        previous = streaming.thread_affinity()
        try:
            streaming.thread_affinity(tuple(mask))
            assert os.sched_getaffinity(0) == {selected}
            entered.set()
            assert release.wait(3)
        except BaseException as exc:
            errors.append(exc)
        finally:
            streaming.thread_affinity(previous)
            if streaming.thread_affinity() != previous:
                errors.append(AssertionError('thread affinity was not restored'))
            entered.set()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(3)
        assert streaming.thread_affinity() == main_mask
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not errors


@pytest.mark.parametrize('mask', [(), (0,)*128, (5,)*127])
def test_invalid_mask_does_not_change_thread(mask):
    before = streaming.thread_affinity()
    with pytest.raises(ValueError):
        streaming.thread_affinity(mask)
    assert streaming.thread_affinity() == before
