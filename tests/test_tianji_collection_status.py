"""Collection summary lifecycle; in-memory episodes only, no hardware."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from teleop_control import tianji_collection
from teleop_control.tianji_collection import TianjiCollection, CAMERA_MAPPING
from FusionDataCollector.collector import Episode


def test_elapsed_freezes_at_stop_through_saving_and_next_session(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(tianji_collection.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(tianji_collection.time, 'monotonic_ns', lambda: round(clock[0]*1e9))
    monkeypatch.setattr(tianji_collection, 'current_platform_username', lambda: 'test')
    manager = Mock()
    collection = TianjiCollection(manager, output=tmp_path)
    collection.arm({'task':'test'})
    assert collection.snapshot()['completed'] == 0
    assert collection.snapshot()['elapsed_s'] == 0
    alive = [True]
    collection.job = SimpleNamespace(is_alive=lambda: alive[0])

    def new_episode(index):
        ep = Episode(SimpleNamespace(), CAMERA_MAPPING, {'sequence':index,'session':'test','button_press_counts':{}})
        ep.directory = tmp_path/str(index)
        ep.directory.mkdir()
        collection.episode = ep
        return ep

    ep = new_episode(1)
    clock[0] = 105.
    assert collection.snapshot()['elapsed_s'] == 0  # Preparation is not recording.
    ep.started_monotonic, ep.state = clock[0], 'recording'
    clock[0] = 117.4
    assert collection.snapshot()['recording']
    assert collection.snapshot()['elapsed_s'] == pytest.approx(12.4)
    collection.stop()
    clock[0] = 140.
    ep.recording_ended_monotonic = clock[0]  # Stream cleanup arrives later than B.
    snap = collection.snapshot()
    assert not snap['recording'] and snap['state'] == 'finalizing'
    assert snap['elapsed_s'] == pytest.approx(12.4)
    assert snap['completed'] == 0
    collection.stop()  # Repeated stop cannot restart the clock.
    assert collection.snapshot()['elapsed_s'] == pytest.approx(12.4)

    # Complete the same worker path, with valid metadata and no camera access.
    ep.stopping.clear()
    def finish():
        (ep.directory/'meta_info.json').write_text(json.dumps({'complete':True,'actions_recorded':True}))
        ep.finished.set()
    ep.run = finish
    collection._record(ep, {'author':'test','teleop_session_id':'test','upload_sides':['left','right']})
    alive[0] = False
    clock[0] = 180.
    assert collection.snapshot()['completed'] == 1
    assert collection.snapshot()['elapsed_s'] == pytest.approx(12.4)
    assert not collection.snapshot()['recording']

    alive[0] = True
    second = new_episode(2)
    assert collection.snapshot()['elapsed_s'] == 0
    assert collection.snapshot()['completed'] == 1
    second.started_monotonic, second.state = clock[0], 'recording'
    clock[0] += 3.
    second.recording_ended_monotonic = clock[0]  # Natural end without B.
    clock[0] += 20.
    assert collection.snapshot()['elapsed_s'] == 3.
    assert not collection.snapshot()['recording']
    second.run = lambda: None
    second.error = 'synthetic failure'
    collection._record(second, {})
    alive[0] = False
    assert collection.snapshot()['completed'] == 1  # Failed recordings do not count.
    assert collection.snapshot()['elapsed_s'] == 3.
    collection.disarm()
    collection.arm({'task':'new session'})
    assert collection.snapshot()['completed'] == 0
    assert collection.snapshot()['elapsed_s'] == 0
    assert not collection.snapshot()['recording']
