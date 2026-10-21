"""Unit tests for the unified single-file config generator.

Covers:
- TestUnifiedConfigInvariants — unified config + dual-runtime assertions
- TestAdaptiveProfiles     — profile pruning over the unified config
- TestNodeRegistry         — registry <-> YAML key bidirectional consistency
- TestCameraModeOverrides  — saved camera mode application
- TestSingleConfig         — build_from_single pipeline edge cases
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
GALAXIS_BRINGUP = REPO_ROOT / "galaxis_bringup"
CONFIG_ROOT = GALAXIS_BRINGUP / "config"
SINGLE_CONFIG = CONFIG_ROOT / "galaxis_config.yaml"

# Make tools/ importable as a flat module, and inner package importable.
for path in (str(GALAXIS_BRINGUP / "tools"), str(GALAXIS_BRINGUP)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _import_merger():
    # Re-import each call to avoid stale module state in long pytest sessions.
    if "build_runtime_configs" in sys.modules:
        importlib.reload(sys.modules["build_runtime_configs"])
    return importlib.import_module("build_runtime_configs")


merger = _import_merger()


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f)


def _build_unified_runtime(tmp_path: Path, profile_file=None) -> Path:
    """Generate runtime files from the real unified config into a tmp dir."""
    out = tmp_path / "_runtime" / "single"
    merger.build_from_single(SINGLE_CONFIG, out, profile_file=profile_file)
    return out



# ---------------------------------------------------------------------------
# TestUnifiedConfigInvariants — unified config structure assertions
# ---------------------------------------------------------------------------
class TestUnifiedConfigInvariants:
    @pytest.mark.parametrize('profile_name,arms', [
        ('dual', ['left', 'right']), ('single-left', ['left']),
        ('single-right', ['right']),
    ])
    @pytest.mark.parametrize('legacy', [False, True])
    def test_episodes_have_no_duration_gate_in_new_or_retained_configs(
        self, tmp_path, profile_name, arms, legacy
    ):
        doc = yaml.safe_load(SINGLE_CONFIG.read_text())
        assert 'duration_limits' not in doc['recording_gate']['groups']
        if legacy:
            for side in ('left', 'right'):
                doc['recording_gate']['groups']['health']['conditions'][f'{side}_gripper_joint_state_timeout_stop']['timeout_sec'] = 0.1
            # Reproduce the installed pre-upgrade config, not just new defaults.
            doc['recording_gate']['groups']['duration_limits'] = {
                'default_type': 'topic_condition',
                'conditions': {
                    'subtask_warn': dict(topic='/task_sequence_manager/subtask_elapsed_sec',
                                         condition='msg.data < 90.0', escalation=1, timeout_sec=-1.0),
                    'subtask_limit': dict(topic='/task_sequence_manager/subtask_elapsed_sec',
                                          condition='msg.data < 120.0', escalation=2, timeout_sec=-1.0),
                },
            }
        single = tmp_path / 'saved.yaml'
        profile_file = tmp_path / 'profile.yaml'
        _write_yaml(single, doc)
        _write_yaml(profile_file, dict(collection_profile=profile_name,
                                       active_arms=arms, active_cameras=arms+['center']))
        out = tmp_path / 'runtime'
        merger.build_from_single(single, out, profile_file)
        gate = yaml.safe_load((out / 'recording_gate.yaml').read_text())
        assert 'duration_limits' not in gate['groups']
        conditions = [c for g in gate['groups'].values() for c in g['conditions'].values()]
        assert not any('elapsed' in c.get('topic', '') for c in conditions)
        # Device/data failure handling is still present and still hard-stops.
        for side in arms:
            for name in [f'{side}_camera_timeout_stop', f'{side}_gripper_joint_state_timeout_stop']:
                assert gate['groups']['health']['conditions'][name]['escalation'] == 2
            assert gate['groups']['vr_tracking']['conditions'][f'{side}_valid_stop']['escalation'] == 2
        assert gate['groups']['tf']['conditions']['pico_hands_stop']['escalation'] == 2
        health = gate['groups']['health']['conditions']
        for side in arms:
            assert health[f'{side}_gripper_joint_state_timeout_stop']['timeout_sec'] == 1.0
            assert health[f'{side}_gripper_joint_state_timeout_warn']['timeout_sec'] == 0.2
            assert health[f'{side}_gripper_joint_state_timeout_warn']['escalation'] == 1
        assert yaml.safe_load(single.read_text()) == doc

    @pytest.fixture(scope="class")
    def portable_runtime(self, tmp_path_factory):
        return _build_unified_runtime(tmp_path_factory.mktemp("dual"))

    def test_offline_mode_and_retention(self):
        with SINGLE_CONFIG.open() as f:
            doc = yaml.safe_load(f)
        params = doc["robot_config"]["/**"]["ros__parameters"]
        assert params["offline_mode"] is True
        assert params["task_file"] == "/opt/data/tasks.yaml"
        assert doc["upload_targets"]["delete_after_upload"] is False
        assert doc["upload_targets"]["local_retention_hours"] == 0

    def test_robot_type(self, portable_runtime):
        with (portable_runtime / "robot_config.yaml").open() as f:
            data = yaml.safe_load(f)
        assert data["/**"]["ros__parameters"]["robot_type"] == "galaxis_portable"

    def test_record_topics_contain_center_camera(self, portable_runtime):
        with (portable_runtime / "robot_config.yaml").open() as f:
            data = yaml.safe_load(f)
        rt = data["/**"]["ros__parameters"]["record_topics"]
        assert "/center_camera/camera_info" in rt
        assert "/center_camera/image_raw/compressed" in rt

    def test_record_topics_exclude_realsense(self, portable_runtime):
        with (portable_runtime / "robot_config.yaml").open() as f:
            data = yaml.safe_load(f)
        rt = data["/**"]["ros__parameters"]["record_topics"]
        for entry in rt:
            assert not entry.startswith("/camera/camera/"), (
                f"portable record_topics should not include RealSense topic {entry}"
            )

    def test_recording_gate_head_camera_topic(self, portable_runtime):
        with (portable_runtime / "recording_gate.yaml").open() as f:
            data = yaml.safe_load(f)
        conditions = data["groups"]["health"]["conditions"]
        for key in (
            "head_camera_warn",
            "head_camera_stop",
            "head_camera_timeout_warn",
            "head_camera_timeout_stop",
        ):
            assert conditions[key]["topic"] == "/center_camera/camera_info", (
                f"portable {key}.topic should be /center_camera/camera_info"
            )
        assert conditions["head_camera_warn"]["min_rate_hz"] == 15.0
        assert conditions["head_camera_stop"]["min_rate_hz"] == 10.0

    def test_galaxis_devices_has_center_camera(self):
        with SINGLE_CONFIG.open() as f:
            data = yaml.safe_load(f)["galaxis_devices"]
        assert "center_camera/usb_cam" in data
        cc = data["center_camera/usb_cam"]["ros__parameters"]
        assert cc["video_device"] == "/dev/galaxis_center_camera"
        assert cc["camera_name"] == "center_camera"
        flip = data["center_camera_flip_node"]["ros__parameters"]
        assert flip["vertical_flip"] is False
        assert flip["jpeg_quality"] == 90

        rates = {
            side: data[f"{side}_camera/usb_cam"]["ros__parameters"]["framerate"]
            for side in ("left", "right", "center")
        }
        assert rates == {"left": 30.0, "right": 30.0, "center": 30.0}

    def test_encoder_uses_role_symlinks_and_legacy_baud(self):
        with SINGLE_CONFIG.open() as f:
            params = yaml.safe_load(f)["galaxis_devices"]["encoder_node"][
                "ros__parameters"
            ]
        assert params["left_port"] == "/dev/galaxis_left_esp32c6"
        assert params["right_port"] == "/dev/galaxis_right_esp32c6"
        assert params["baud"] == 115200
        assert params["left_rotation_direction"] == -1
        assert params["right_rotation_direction"] == 1

    def test_center_camera_info_keeps_reliable_qos(self):
        source_path = GALAXIS_BRINGUP / "galaxis_bringup" / "center_camera_flip_node.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        camera_info_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"create_publisher", "create_subscription"}
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "CameraInfo"
        ]
        assert len(camera_info_calls) == 2
        for call in camera_info_calls:
            qos_index = 2 if call.func.attr == "create_publisher" else 3
            assert isinstance(call.args[qos_index], ast.Name)
            assert call.args[qos_index].id == "camera_info_qos"
        assert "reliability=ReliabilityPolicy.RELIABLE" in source

    def test_center_public_raw_image_matches_web_video_reliable_qos(self):
        source_path = GALAXIS_BRINGUP / "galaxis_bringup" / "center_camera_flip_node.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        raw_publishers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_publisher"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "Image"
        ]
        assert len(raw_publishers) == 1
        qos_arg = raw_publishers[0].args[2]
        assert isinstance(qos_arg, ast.Name)
        assert qos_arg.id == "output_image_qos"

    def test_portable_no_undeclared_gripper_params(self):
        """Unified config must not set gripper-state event params until the
        submodule's task_command_dispatch_node declares them (rclpy errors on
        undeclared params)."""
        with SINGLE_CONFIG.open() as f:
            data = yaml.safe_load(f)["galaxis_devices"]
        dispatch = data.get("task_command_dispatch_node", {}).get("ros__parameters", {})
        for key in dispatch.keys():
            assert not key.startswith("gripper_"), (
                f"unified config has premature gripper param: {key}"
            )

    def test_portable_no_footpedal_node(self):
        """Portable hardware has no footpedal, so the unified config omits it."""
        with SINGLE_CONFIG.open() as f:
            data = yaml.safe_load(f)["galaxis_devices"]
        assert "footpedal_node" not in data


class TestAdaptiveProfiles:
    def test_dual_profile_preserves_three_cameras_and_two_arms(self, tmp_path):
        profile_file = tmp_path / "collection_profile.yaml"
        _write_yaml(profile_file, merger.PROFILE_DEFAULT)
        runtime = _build_unified_runtime(tmp_path, profile_file)
        devices = yaml.safe_load((runtime / "galaxis_devices.yaml").read_text())
        topics = yaml.safe_load((runtime / "robot_config.yaml").read_text())[
            "/**"
        ]["ros__parameters"]["record_topics"]
        assert {
            "left_camera/usb_cam",
            "right_camera/usb_cam",
            "center_camera/usb_cam",
            "left_camera_brightness_controller",
            "right_camera_brightness_controller",
            "center_camera_brightness_controller",
        } <= set(devices)
        assert devices["encoder_node"]["ros__parameters"]["active_sides"] == [
            "left",
            "right",
        ]
        for side in ("left", "right", "center"):
            assert f"/{side}_camera/image_raw/compressed" in topics

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_single_arm_runtime_contract(self, tmp_path, side):
        profile_file = tmp_path / "collection_profile.yaml"
        _write_yaml(
            profile_file,
            {
                "collection_profile": f"single-{side}",
                "active_cameras": [side, "center"],
                "active_arms": [side],
            },
        )
        merger.build_from_single(
            SINGLE_CONFIG,
            tmp_path / "out",
            profile_file=profile_file,
        )
        runtime = tmp_path / "out"
        devices = yaml.safe_load((runtime / "galaxis_devices.yaml").read_text())
        topics = yaml.safe_load((runtime / "robot_config.yaml").read_text())[
            "/**"
        ]["ros__parameters"]["record_topics"]
        conditions = yaml.safe_load((runtime / "recording_gate.yaml").read_text())[
            "groups"
        ]
        other = "right" if side == "left" else "left"
        assert f"{side}_camera/usb_cam" in devices
        assert f"{other}_camera/usb_cam" not in devices
        assert f"{side}_camera_brightness_controller" in devices
        assert f"{other}_camera_brightness_controller" not in devices
        assert "center_camera/usb_cam" in devices
        assert devices["encoder_node"]["ros__parameters"]["active_sides"] == [side]
        assert f"/{side}_camera/image_raw/compressed" in topics
        assert f"/{other}_camera/image_raw/compressed" not in topics
        assert "/collection_profile" in topics
        serialized_conditions = yaml.safe_dump(conditions)
        assert f"/{other}_camera/" not in serialized_conditions
        assert f"/galaxis/gripper/{other}/" not in serialized_conditions

    def test_invalid_profile_is_rejected(self, tmp_path):
        profile_file = tmp_path / "collection_profile.yaml"
        _write_yaml(
            profile_file,
            {
                "collection_profile": "single-left",
                "active_cameras": ["right", "center"],
                "active_arms": ["left"],
            },
        )
        with pytest.raises(ValueError, match="invalid collection profile"):
            merger.build_from_single(
                SINGLE_CONFIG, tmp_path / "out", profile_file=profile_file
            )

    def test_g2_center_source_replaces_only_usb_center_nodes(self, tmp_path):
        profile_file = tmp_path / "collection_profile.yaml"
        _write_yaml(
            profile_file,
            {
                "collection_profile": "single-right",
                "active_cameras": ["right", "center"],
                "active_arms": ["right"],
                "camera_sources": {"center": "g2_head_color"},
                "cameras": {
                    "right": {
                        "image_width": 640,
                        "image_height": 480,
                        "framerate": 30.0,
                        "pixel_format": "yuyv2rgb",
                    },
                    "center": {
                        "image_width": 640,
                        "image_height": 400,
                        "framerate": 30.0,
                        "pixel_format": "mjpeg2rgb",
                    },
                },
            },
        )
        runtime = _build_unified_runtime(tmp_path, profile_file)
        devices = yaml.safe_load((runtime / "galaxis_devices.yaml").read_text())
        assert "right_camera/usb_cam" in devices
        assert "center_camera/g2_bridge" in devices
        assert "center_camera/usb_cam" not in devices
        assert "center_camera_brightness_controller" not in devices
        assert "center_camera_flip_node" in devices

    def test_unknown_center_source_is_rejected(self, tmp_path):
        profile_file = tmp_path / "collection_profile.yaml"
        _write_yaml(
            profile_file,
            {
                **merger.PROFILE_DEFAULT,
                "camera_sources": {"center": "unknown"},
            },
        )
        with pytest.raises(ValueError, match="unsupported center camera source"):
            merger.load_collection_profile(profile_file)


# ---------------------------------------------------------------------------
# TestNodeRegistry — registry <-> YAML key bidirectional consistency
# ---------------------------------------------------------------------------
class TestNodeRegistry:
    @pytest.fixture(scope="class")
    def keys_per_variant(self):
        with SINGLE_CONFIG.open() as f:
            data = yaml.safe_load(f) or {}
        return {"unified": set(data["galaxis_devices"].keys())}

    def test_registry_entries_have_yaml_coverage(self, keys_per_variant):
        from galaxis_bringup.launch_registry import NODE_REGISTRY  # noqa: WPS433

        all_keys = set().union(*keys_per_variant.values())
        # Stationary-only hardware (RealSense head camera + footpedal) was
        # retired together with the variant config directories. Its registry
        # entries remain for code compatibility but intentionally have no YAML
        # coverage in the unified config.
        retired_stationary = {"/camera/camera", "footpedal_node"}
        unreachable = [
            entry["yaml_key"]
            for entry in NODE_REGISTRY
            if entry["yaml_key"] not in all_keys
            and entry["yaml_key"] not in retired_stationary
        ]
        assert not unreachable, (
            f"NODE_REGISTRY entries with no YAML coverage (would never launch): "
            f"{unreachable}"
        )

    def test_yaml_keys_covered_by_registry(self, keys_per_variant):
        from galaxis_bringup.launch_registry import (  # noqa: WPS433
            NODE_REGISTRY,
            STRUCTURAL_NODE_KEYS,
        )

        registry_keys = {entry["yaml_key"] for entry in NODE_REGISTRY}
        all_yaml_keys = set().union(*keys_per_variant.values())
        orphan = all_yaml_keys - registry_keys - STRUCTURAL_NODE_KEYS
        assert not orphan, (
            f"YAML keys with no NODE_REGISTRY entry (parameters loaded but no node "
            f"will spawn): {orphan}"
        )

    def test_bringup_and_data_collection_disjoint(self):
        from galaxis_bringup.launch_registry import (  # noqa: WPS433
            BRINGUP_NODE_REGISTRY,
            DATA_COLLECTION_NODE_REGISTRY,
        )

        bringup_keys = {e["yaml_key"] for e in BRINGUP_NODE_REGISTRY}
        dc_keys = {e["yaml_key"] for e in DATA_COLLECTION_NODE_REGISTRY}
        overlap = bringup_keys & dc_keys
        assert not overlap, (
            f"Same yaml_key registered in both bringup and data-collection "
            f"registries (would double-spawn): {overlap}"
        )

    def test_collect_yaml_keys_helper(self, tmp_path):
        from galaxis_bringup.launch_registry import collect_yaml_keys  # noqa: WPS433

        f1 = tmp_path / "a.yaml"
        _write_yaml(f1, {"x": 1, "y": 2})
        f2 = tmp_path / "b.yaml"
        _write_yaml(f2, {"y": 3, "z": 4})  # y is shared
        assert collect_yaml_keys([f1, f2, tmp_path / "missing.yaml"]) == {"x", "y", "z"}

    def test_center_camera_all_transports_are_private_before_flip(self):
        from galaxis_bringup.launch_registry import camera_remappings

        remappings = dict(camera_remappings("center_camera"))
        assert remappings["image_raw"] == "image_raw_unflipped"
        assert (
            remappings["image_raw/compressed"]
            == "image_raw_unflipped/compressed"
        )
        assert remappings["camera_info"] == "camera_info_unflipped"
        assert camera_remappings("left_camera") == []

    def test_center_camera_saved_settings_override_runtime_defaults(self, tmp_path):
        from galaxis_bringup.launch_registry import with_center_camera_settings

        saved = tmp_path / "center_camera.yaml"
        saved.write_text(
            "center_camera_brightness_controller:\n"
            "  ros__parameters:\n"
            "    brightness: -42\n"
        )
        params = with_center_camera_settings(
            [{"brightness": 0}], saved_setting=saved
        )
        assert params == [{"brightness": 0}, str(saved)]


class TestCameraModeOverrides:
    def test_profile_camera_modes_override_every_present_camera(self, tmp_path):
        profile = tmp_path / "collection_profile.yaml"
        _write_yaml(
            profile,
            {
                "cameras": {
                    "left": {
                        "image_width": 1280,
                        "image_height": 720,
                        "framerate": 60.0,
                        "pixel_format": "mjpeg2rgb",
                    },
                    "center": {
                        "image_width": 320,
                        "image_height": 240,
                        "framerate": 15.0,
                        "pixel_format": "yuyv2rgb",
                    },
                }
            },
        )
        overrides = merger.load_camera_overrides(profile)
        data = {
            "left_camera/usb_cam": {"ros__parameters": {}},
            "right_camera/usb_cam": {"ros__parameters": {}},
            "center_camera/usb_cam": {"ros__parameters": {}},
        }

        result = merger.apply_camera_overrides(data, overrides)

        assert result["left_camera/usb_cam"]["ros__parameters"] == {
            "image_width": 1280,
            "image_height": 720,
            "framerate": 60.0,
            "pixel_format": "mjpeg2rgb",
        }
        assert result["center_camera/usb_cam"]["ros__parameters"] == {
            "image_width": 320,
            "image_height": 240,
            "framerate": 15.0,
            "pixel_format": "yuyv2rgb",
        }
        assert result["right_camera/usb_cam"]["ros__parameters"] == {}

    @pytest.mark.parametrize("pixel_format", ["mjpeg2rgb"])
    def test_center_mjpeg_selection_survives_runtime_generation(self, tmp_path, pixel_format):
        profile = tmp_path / "collection_profile.yaml"
        mode = {"image_width": 640, "image_height": 480,
                "framerate": 30.0, "pixel_format": pixel_format}
        _write_yaml(profile, {"cameras": {"center": mode}})
        output = _build_unified_runtime(tmp_path, profile)
        data = yaml.safe_load((output / "galaxis_devices.yaml").read_text())
        params = data["center_camera/usb_cam"]["ros__parameters"]
        assert {key: params[key] for key in mode} == mode

    @pytest.mark.parametrize(
        "field,value",
        [
            ("image_width", 0),
            ("framerate", -1),
            ("pixel_format", "unsupported"),
        ],
    )
    def test_invalid_saved_camera_mode_is_rejected(self, tmp_path, field, value):
        settings = {
            "image_width": 640,
            "image_height": 480,
            "framerate": 30.0,
            "pixel_format": "yuyv2rgb",
        }
        settings[field] = value
        profile = tmp_path / "collection_profile.yaml"
        _write_yaml(profile, {"cameras": {"left": settings}})

        with pytest.raises(ValueError, match="camera left"):
            merger.load_camera_overrides(profile)


class TestSingleConfig:
    def test_build_from_single_writes_all_sections(self, tmp_path):
        single = tmp_path / "galaxis_config.yaml"
        _write_yaml(
            single,
            {
                "robot_config": {
                    "/**": {
                        "ros__parameters": {
                            "record_topics": [
                                "/galaxis/gripper/left/pose",
                                "/pico/controller/left/pose",
                                "/collection_profile",
                            ],
                            "offline_mode": True,
                            "task_file": "/opt/data/tasks.yaml",
                        }
                    }
                },
                "galaxis_devices": {
                    "left_camera/usb_cam": {"ros__parameters": {}},
                    "right_camera/usb_cam": {"ros__parameters": {}},
                    "encoder_node": {"ros__parameters": {"active_sides": []}},
                },
                "recording_gate": {"groups": {}},
                "upload_targets": {
                    "delete_after_upload": False,
                    "local_retention_hours": 0,
                },
            },
        )
        out_dir = tmp_path / "runtime"

        written = merger.build_from_single(single, out_dir, profile_file=None)

        assert {p.name for p in written} == {
            "robot_config.yaml",
            "galaxis_devices.yaml",
            "recording_gate.yaml",
            "upload_targets.yaml",
            "collection_profile.yaml",
        }
        robot = yaml.safe_load((out_dir / "robot_config.yaml").read_text())
        params = robot["/**"]["ros__parameters"]
        assert params["offline_mode"] is True
        assert params["task_file"] == "/opt/data/tasks.yaml"
        assert "/collection_profile" in params["record_topics"]
        devices = yaml.safe_load(
            (out_dir / "galaxis_devices.yaml").read_text()
        )
        assert devices["encoder_node"]["ros__parameters"]["active_sides"] == [
            "left",
            "right",
        ]
        uploads = yaml.safe_load(
            (out_dir / "upload_targets.yaml").read_text()
        )
        assert uploads["delete_after_upload"] is False
        assert uploads["local_retention_hours"] == 0

    def test_missing_section_rejected(self, tmp_path):
        single = tmp_path / "galaxis_config.yaml"
        _write_yaml(single, {"robot_config": {}})

        with pytest.raises(ValueError, match="missing section"):
            merger.build_from_single(
                single, tmp_path / "out", profile_file=None
            )

@pytest.mark.parametrize('source,expected', [('usb','compressed'), ('g2_head_color','raw')])
def test_center_runtime_parameters_reach_launched_node(tmp_path, monkeypatch, source, expected):
    from types import ModuleType
    import galaxis_bringup.launch_registry as registry
    module = ModuleType('launch_ros.actions')
    module.Node = lambda **kw: kw
    monkeypatch.setitem(sys.modules, 'launch_ros.actions', module)
    monkeypatch.setattr(registry, 'with_center_camera_settings', lambda params: list(params))
    profile_file = tmp_path/'profile.yaml'
    _write_yaml(profile_file, {'camera_sources': {'center': source}})
    runtime = _build_unified_runtime(tmp_path, profile_file=profile_file)/'galaxis_devices.yaml'
    node = registry._factory_center_camera_flip([str(runtime)])
    assert node['name'] == 'center_camera_flip'
    # Only actual-node sections and parameter dictionaries apply in ROS. The
    # old executable-name section was silently ignored in the real launch.
    effective = {}
    for param in node['parameters']:
        if isinstance(param, dict): effective.update(param)
        else: effective.update(yaml.safe_load(Path(param).read_text()).get(node['name'], {}).get('ros__parameters', {}))
    assert effective['input_transport'] == expected
    assert effective['vertical_flip'] is False
    assert effective['jpeg_quality'] == 90
