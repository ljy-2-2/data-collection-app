from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'umi_collection/vendor/galaxis/monitor/bridge'))
from galaxis_studio_bridge.config import Settings
from galaxis_studio_bridge.ros_state import RosStateCollector


def test_monitor_uses_recorder_duration_including_stopped_round_not_subtask_timer():
    state = RosStateCollector(Settings(profile_path='/nonexistent'))
    state._on_sample('episode_elapsed', 999.0)
    state._on_sample('recording_elapsed', 12.75)
    state._on_sample('recording', True)
    assert state.snapshot()['episode']['elapsed_sec'] == 12.75
    state._on_sample('recording', False)
    state._on_sample('episode_elapsed', 0.0)
    assert state.snapshot()['episode']['elapsed_sec'] == 12.75
    state._on_sample('recording_elapsed', 0.0)
    state._on_sample('recording', True)
    assert state.snapshot()['episode']['elapsed_sec'] == 0.0
