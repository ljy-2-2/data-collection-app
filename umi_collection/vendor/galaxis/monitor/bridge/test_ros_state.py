"""Focused tests for collection-monitor state and sensor precision."""

import json
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from galaxis_studio_bridge.config import Settings
from galaxis_studio_bridge.main import _RECORD_SERVICE, _import_msg_type
from galaxis_studio_bridge.ros_state import RosStateCollector, _parse_joint_state


class TestEncoderPrecision(unittest.TestCase):
    def test_joint_position_keeps_recorded_precision_through_monitor_snapshot(self):
        collector = RosStateCollector(Settings(profile_path=""))
        for position in (0.0, 1e-15, 0.21545957173777436, 2.345678901234567, -1e-9):
            for side in ("left", "right"):
                with self.subTest(position=position, side=side):
                    message = SimpleNamespace(
                        name=[f"{side}_joint"], position=[position], velocity=[]
                    )
                    parsed = _parse_joint_state(message)
                    self.assertEqual(parsed["position"], position)
                    collector._on_sample(f"encoder_{side}", parsed)

                    # HTTP and WebSocket responses both serialize this snapshot.
                    snapshot = json.loads(json.dumps(collector.snapshot()))
                    self.assertEqual(snapshot["encoders"][side]["position"], position)

    def test_empty_joint_position_retains_zero_fallback(self):
        self.assertEqual(_parse_joint_state(SimpleNamespace())["position"], 0.0)


class TestCameraAlerts(unittest.TestCase):
    def test_ros_byte_zero_is_healthy_and_recovery_clears_alerts(self):
        collector = RosStateCollector(Settings(profile_path=""))
        def send(level, message):
            entries = [SimpleNamespace(name='recording_gate/camera_quality/center_image_30fps', level=level, message=message),
                       SimpleNamespace(name='recording_gate/camera_quality/center_image_sustained_failure', level=level, message=message),
                       SimpleNamespace(name='recording_gate/camera_quality', level=b'\x01', message='settled with failures')]
            collector._on_camera_quality(SimpleNamespace(status=entries))
            return collector.snapshot()['camera_quality_issues']
        self.assertEqual(send(b'\x00', 'ok'), [])
        self.assertEqual(send(b'\x01', '图像已停顿'), ['中心相机：图像已停顿'])
        self.assertEqual(send(0, 'ok'), [])
        self.assertEqual(send(2, '帧间隔超限'), ['中心相机：帧间隔超限'])
        self.assertEqual(send(bytearray([0]), 'ok'), [])


class TestEpisodeCounting(unittest.TestCase):
    def setUp(self):
        self.collector = RosStateCollector(Settings(profile_path=""))

    def test_saved_round_increments_before_hdf5_conversion_completes(self):
        self.collector._on_sample("recording_saved", "/data/hdf5/Galaxis/round_001")

        self.assertEqual(
            self.collector.snapshot()["episode"]["completed_total"], 1
        )

    def test_upload_ready_event_does_not_double_count_same_round(self):
        path = "/data/hdf5/Galaxis/round_001"
        self.collector._on_sample("recording_saved", path)
        self.collector._on_sample("recording_completed", path)

        self.assertEqual(
            self.collector.snapshot()["episode"]["completed_total"], 1
        )

    def test_distinct_rounds_each_increment_once(self):
        self.collector._on_sample("recording_saved", "/data/rosbags/Galaxis/round_001")
        self.collector._on_sample("recording_completed", "/data/rosbags/Galaxis/round_001")
        self.collector._on_sample("recording_saved", "/data/hdf5/Galaxis/round_002")

        self.assertEqual(
            self.collector.snapshot()["episode"]["completed_total"], 2
        )


class TestVrPoseUpdates(unittest.TestCase):
    def setUp(self):
        self.collector = RosStateCollector(Settings(profile_path=""))

    def test_nonzero_controller_pose_updates_even_when_tracking_flag_is_false(self):
        first = {
            "position": [0.1, 0.2, 0.3],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "tracked": True,
            "valid": True,
        }
        moved = {
            "position": [0.4, 0.5, 0.6],
            "rotation": [0.0, 0.1, 0.0, 0.99],
            "tracked": False,
            "valid": False,
        }
        self.collector._on_sample("vr_left", first)
        self.collector._on_sample("vr_left", moved)

        reading = self.collector.snapshot()["vr"]["left"]
        self.assertEqual(reading["position"], moved["position"])
        self.assertEqual(reading["rotation"], moved["rotation"])

    def test_zero_controller_pose_keeps_last_nonzero_pose(self):
        first = {
            "position": [0.1, 0.2, 0.3],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "tracked": True,
            "valid": True,
        }
        zero = {
            "position": [0.0, 0.0, 0.0],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "tracked": False,
            "valid": False,
        }
        self.collector._on_sample("vr_right", first)
        self.collector._on_sample("vr_right", zero)

        reading = self.collector.snapshot()["vr"]["right"]
        self.assertEqual(reading["position"], first["position"])

    def test_old_vr_and_encoder_values_are_not_reported_online(self):
        now = time.time()
        self.collector._state["vr_hmd"] = {"position": [0.1, 0.2, 0.3]}
        self.collector._state["encoder_left"] = {"position": 0.2}
        for key in ("vr_hmd", "encoder_left"):
            self.collector._rates[key].tick(now - 0.1)
            self.collector._rates[key].tick(now)
        snapshot = self.collector.snapshot()
        self.assertTrue(snapshot["vr"]["hmd"]["online"])
        self.assertTrue(snapshot["encoders"]["left"]["online"])

        for key in ("vr_hmd", "encoder_left"):
            self.collector._rates[key]._stamps.clear()
        snapshot = self.collector.snapshot()
        self.assertFalse(snapshot["vr"]["hmd"]["online"])
        self.assertFalse(snapshot["encoders"]["left"]["online"])


class TestServiceTypeImport(unittest.TestCase):
    def test_imports_three_part_ros_service_type(self):
        expected = object()
        module = SimpleNamespace(StringTrigger=expected)
        with patch("builtins.__import__", return_value=module) as importer:
            actual = _import_msg_type("airoa_data_msgs/srv/StringTrigger")

        self.assertIs(actual, expected)
        importer.assert_called_once_with(
            "airoa_data_msgs.srv", fromlist=["StringTrigger"]
        )

    def test_web_recording_uses_same_round_services_as_controller_buttons(self):
        self.assertEqual(
            _RECORD_SERVICE,
            {
                "start": ("/data_collection/accept", "std_srvs/srv/Trigger"),
                "stop": ("/data_collection/next_round", "std_srvs/srv/Trigger"),
                "cancel": ("/data_collection/cancel_episode", "std_srvs/srv/Trigger"),
            },
        )


if __name__ == "__main__":
    unittest.main()
