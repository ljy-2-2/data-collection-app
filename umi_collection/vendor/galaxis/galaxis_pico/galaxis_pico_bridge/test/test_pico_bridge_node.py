"""Unit tests for ``galaxis_pico_bridge.pico_bridge_node``.

Covers:
    * Pure-math helpers (PICO SDK native -> ROS conversion, quaternion ops).
    * Field extraction helpers (``_get_xyz`` / ``_get_xyzw``).
    * The new per-sensor publishing path (``PicoHmd`` / ``PicoController``)
      driven through a synthetic ``PicoFrame``.
"""

from __future__ import annotations

import math

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


@pytest.fixture()
def node_module(mock_ros):
    """Import the bridge node after the ROS mocks are installed."""
    import galaxis_pico_bridge.pico_bridge_node as mod

    return mod


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def test_pico_pos_to_ros_axis_mapping(node_module):
    """PICO (X right, Y up, Z back) -> ROS (X forward, Y left, Z up)."""
    assert node_module._pico_pos_to_ros(0.0, 0.0, -1.0) == (1.0, 0.0, 0.0)
    assert node_module._pico_pos_to_ros(1.0, 0.0, 0.0) == (0.0, -1.0, 0.0)
    assert node_module._pico_pos_to_ros(0.0, 1.0, 0.0) == (0.0, 0.0, 1.0)


def test_normalize_quat_makes_unit_length(node_module):
    qx, qy, qz, qw = node_module._normalize_quat(2.0, 0.0, 0.0, 2.0)
    assert qx**2 + qy**2 + qz**2 + qw**2 == pytest.approx(1.0)


def test_normalize_quat_zero_returns_identity(node_module):
    """A zero-norm input must not produce NaN; identity is the safe fallback."""
    assert node_module._normalize_quat(0.0, 0.0, 0.0, 0.0) == (0.0, 0.0, 0.0, 1.0)


def test_pico_quat_to_ros_sign_pattern(node_module):
    """PICO native -> ROS: (x,y,z,w) -> (-z,-x,y,w)."""
    assert node_module._pico_quat_to_ros(1.0, 2.0, 3.0, 4.0) == (-3.0, -1.0, 2.0, 4.0)


@pytest.mark.parametrize(
    ("pico_axis_quat", "ros_axis_quat"),
    [
        ((1.0, 0.0, 0.0, 1.0), (0.0, -1.0, 0.0, 1.0)),
        ((0.0, 1.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)),
        ((0.0, 0.0, 1.0, 1.0), (-1.0, 0.0, 0.0, 1.0)),
    ],
)
def test_pico_quaternion_rotation_axes_follow_position_basis(
    node_module, pico_axis_quat, ros_axis_quat
):
    """Native X/Y/Z rotation axes map to ROS -Y/+Z/-X respectively."""
    converted = node_module._normalize_quat(
        *node_module._pico_quat_to_ros(*pico_axis_quat)
    )
    expected = node_module._normalize_quat(*ros_axis_quat)
    assert converted == pytest.approx(expected)


def test_pico_pose_to_ros_returns_normalized_quat(node_module):
    pos, quat = node_module._pico_pose_to_ros((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 2.0))
    assert pos == (-3.0, -1.0, 2.0)
    assert sum(c * c for c in quat) == pytest.approx(1.0)


def test_quat_multiply_identity_left_and_right(node_module):
    q = (0.1, 0.2, 0.3, math.sqrt(1.0 - 0.14))
    identity = (0.0, 0.0, 0.0, 1.0)
    left = node_module._quat_multiply(identity, q)
    right = node_module._quat_multiply(q, identity)
    for got in (left, right):
        for a, b in zip(got, q):
            assert a == pytest.approx(b, abs=1e-6)


def test_invert_then_compose_yields_identity_pose(node_module):
    """Composing a transform with its inverse should recover the identity."""
    parent_t = (0.5, -0.3, 1.2)
    parent_q = (0.0, 0.0, 0.0, 1.0)
    inv_t, inv_q = node_module._invert_transform(parent_t, parent_q)

    composed_t, composed_q = node_module._compose_transforms(
        parent_t, parent_q, inv_t, inv_q
    )
    for v in composed_t:
        assert v == pytest.approx(0.0, abs=1e-6)
    assert composed_q == pytest.approx((0.0, 0.0, 0.0, 1.0), abs=1e-6)


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def test_get_xyz_parses_well_formed_dict(node_module):
    assert node_module._get_xyz({"x": 1, "y": 2, "z": 3}) == (1.0, 2.0, 3.0)


def test_get_xyz_returns_none_on_missing_or_bad_input(node_module):
    assert node_module._get_xyz(None) is None
    assert node_module._get_xyz({}) is None
    assert node_module._get_xyz({"x": "not numeric", "y": 0, "z": 0}) is None


def test_get_xyzw_parses_quaternion_dict(node_module):
    assert node_module._get_xyzw({"x": 0, "y": 0, "z": 0, "w": 1}) == (
        0.0,
        0.0,
        0.0,
        1.0,
    )


def test_get_xyzw_returns_none_on_missing_w(node_module):
    assert node_module._get_xyzw({"x": 0, "y": 0, "z": 0}) is None


# ---------------------------------------------------------------------------
# Controller-key map symmetry
# ---------------------------------------------------------------------------


def test_controller_key_map_left_and_right_share_field_set(node_module):
    """Both sides must expose the same set of logical fields."""
    assert set(node_module._CONTROLLER_KEYS["left"].keys()) == set(
        node_module._CONTROLLER_KEYS["right"].keys()
    )


def test_controller_key_map_does_not_mix_sides(node_module):
    """Sanity: a left-side key should never reference a right-side raw field."""
    for raw_key in node_module._CONTROLLER_KEYS["left"].values():
        assert "right" not in raw_key.lower()
    for raw_key in node_module._CONTROLLER_KEYS["right"].values():
        assert "left" not in raw_key.lower()


# ---------------------------------------------------------------------------
# Per-sensor publish path
# ---------------------------------------------------------------------------


def _make_node(node_module):
    """Construct a ``PicoBridgeNode`` against the mocked ROS layer."""
    return node_module.PicoBridgeNode()


def _make_frame(node_module, **overrides):
    """Build a synthetic ``PicoFrame`` with a typical full payload."""
    from galaxis_pico_bridge.transport.galaxis_tcp import PicoFrame

    raw = {
        "deviceTimeNs": 1_500_000_000,
        "deltaTime": 0.02,
        "hmdPosition": {"x": 0.1, "y": 0.2, "z": 0.3},
        "hmdRotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "leftControllerPosition": {"x": -0.2, "y": 1.0, "z": 0.5},
        "leftControllerRotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "rightControllerPosition": {"x": 0.2, "y": 1.0, "z": 0.5},
        "rightControllerRotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "leftJoystick": {"x": 0.4, "y": -0.5},
        "rightJoystick": {"x": -0.1, "y": 0.2},
        "leftTracked": True,
        "leftValid": True,
        "rightTracked": True,
        "rightValid": False,
        "leftThumbstickClick": True,
        "leftThumbstickTouched": True,
        "rightThumbstickClick": False,
        "rightThumbstickTouched": True,
        "leftTriggerPressed": True,
        "leftIndexTriggerTouched": True,
        "rightTriggerPressed": False,
        "rightIndexTriggerTouched": True,
        "leftGripPressed": True,
        "rightGripPressed": False,
        "buttonXPressed": True,
        "buttonXTouched": True,
        "buttonYPressed": False,
        "buttonYTouched": True,
        "buttonAPressed": True,
        "buttonATouched": False,
        "buttonBPressed": True,
        "buttonBTouched": True,
        "startPressed": True,
        "backPressed": False,
        "hmdBattPct": 80,
        "leftBattPct": 50,
        "rightBattPct": 60,
        "hmdCharging": False,
    }
    raw.update(overrides.get("raw_overrides", {}))

    return PicoFrame(
        device_time_ns=overrides.get("device_time_ns", 1_500_000_000),
        pc_monotonic_ns=overrides.get("pc_monotonic_ns", 9_000_000_000),
        seq=overrides.get("seq", 0),
        pico_id=overrides.get("pico_id", ""),
        delta_time_s=overrides.get("delta_time_s", 0.02),
        raw=raw,
    )


def test_publish_pico_messages_emits_one_per_sensor(node_module):
    node = _make_node(node_module)
    frame = _make_frame(node_module)

    node._publish_pico_messages(frame, recv_stamp=None)

    assert len(node.published["/pico/hmd/state"]) == 1
    assert len(node.published["/pico/controller/left/state"]) == 1
    assert len(node.published["/pico/controller/right/state"]) == 1


def test_pico_hmd_message_fields_populated_correctly(node_module):
    node = _make_node(node_module)
    frame = _make_frame(node_module)

    node._publish_pico_messages(frame, recv_stamp="STAMP")
    hmd = node.published["/pico/hmd/state"][0]

    assert hmd.header.stamp == "STAMP"
    assert hmd.header.frame_id == "pico_origin"
    assert hmd.device_time_ns == 1_500_000_000
    assert hmd.pc_monotonic_ns == 9_000_000_000
    assert hmd.seq == 0
    assert hmd.pico_id == ""
    # PICO native (0.1, 0.2, 0.3) -> ROS (-0.3, -0.1, 0.2).
    assert hmd.position.x == pytest.approx(-0.3)
    assert hmd.position.y == pytest.approx(-0.1)
    assert hmd.position.z == pytest.approx(0.2)
    # Identity remains identity after the proper right-handed basis rotation.
    assert (
        hmd.rotation.x**2 + hmd.rotation.y**2 + hmd.rotation.z**2 + hmd.rotation.w**2
    ) == pytest.approx(1.0)


def test_pico_left_controller_message_carries_pose_and_tracking(node_module):
    node = _make_node(node_module)
    frame = _make_frame(node_module)

    node._publish_pico_messages(frame, recv_stamp=None)
    left = node.published["/pico/controller/left/state"][0]

    assert left.tracked is True
    assert left.valid is True
    # Pose fields are populated (PICO -> ROS conversion exercised elsewhere).
    assert (
        left.rotation.x**2
        + left.rotation.y**2
        + left.rotation.z**2
        + left.rotation.w**2
    ) == pytest.approx(1.0)


def test_pico_right_controller_message_carries_pose_and_tracking(node_module):
    node = _make_node(node_module)
    frame = _make_frame(node_module)

    node._publish_pico_messages(frame, recv_stamp=None)
    right = node.published["/pico/controller/right/state"][0]

    assert right.tracked is True
    assert right.valid is False


def test_publish_pico_messages_skips_when_pose_missing(node_module):
    """If a controller's pose is absent we should silently skip its publish."""
    node = _make_node(node_module)
    frame = _make_frame(
        node_module,
        raw_overrides={
            "leftControllerPosition": None,
            "leftControllerRotation": None,
        },
    )

    node._publish_pico_messages(frame, recv_stamp=None)

    assert len(node.published["/pico/controller/left/state"]) == 0
    assert len(node.published["/pico/controller/right/state"]) == 1
    # Skipped controllers must not emit stale tracked/valid Bool either.
    assert len(node.published["/pico/controller/left/tracked"]) == 0
    assert len(node.published["/pico/controller/left/valid"]) == 0
    assert len(node.published["/pico/controller/right/tracked"]) == 1
    assert len(node.published["/pico/controller/right/valid"]) == 1


def test_publish_pico_messages_emits_tracked_and_valid_bool(node_module):
    node = _make_node(node_module)
    frame = _make_frame(node_module)

    node._publish_pico_messages(frame, recv_stamp=None)

    # leftTracked=True, leftValid=True, rightTracked=True, rightValid=False
    # in the canonical synthetic frame.
    assert node.published["/pico/controller/left/tracked"][0].data is True
    assert node.published["/pico/controller/left/valid"][0].data is True
    assert node.published["/pico/controller/right/tracked"][0].data is True
    assert node.published["/pico/controller/right/valid"][0].data is False
    assert len(node.published["/pico/hmd/state"]) == 1


def test_publish_pico_messages_propagates_seq_and_pico_id(node_module):
    """When the protocol provides them, ``seq`` and ``pico_id`` flow through."""
    node = _make_node(node_module)
    frame = _make_frame(
        node_module, seq=42, pico_id="abcd-1234", device_time_ns=999, pc_monotonic_ns=1
    )

    node._publish_pico_messages(frame, recv_stamp=None)

    hmd = node.published["/pico/hmd/state"][0]
    assert hmd.seq == 42
    assert hmd.pico_id == "abcd-1234"
    assert hmd.device_time_ns == 999
    assert hmd.pc_monotonic_ns == 1

    for side in (
        "/pico/controller/left/state",
        "/pico/controller/right/state",
    ):
        msg = node.published[side][0]
        assert msg.seq == 42
        assert msg.pico_id == "abcd-1234"
        assert msg.device_time_ns == 999
        assert msg.pc_monotonic_ns == 1


# ---------------------------------------------------------------------------
# Optional robot hand-offset TF lookup
# ---------------------------------------------------------------------------


def test_missing_hand_offset_lookup_is_immediate_and_rate_limited(
    node_module, monkeypatch
):
    """A missing optional TF must never throttle the incoming VR stream."""
    node = _make_node(node_module)
    now = [100.0]
    monkeypatch.setattr(node_module.time, "monotonic", lambda: now[0])
    node._tf_buffer.lookup_transform.side_effect = node_module.LookupException(
        "missing"
    )

    assert node._get_hand_controller_transform("left_hand_root", "pico_left") is None
    assert node._get_hand_controller_transform("left_hand_root", "pico_left") is None

    node._tf_buffer.lookup_transform.assert_called_once()
    # Three arguments means an immediate tf2 cache query; a fourth timeout
    # argument (or timeout keyword) would block the transport callback.
    args, kwargs = node._tf_buffer.lookup_transform.call_args
    assert len(args) == 3
    assert "timeout" not in kwargs

    now[0] += node_module._TF_OFFSET_RETRY_S + 0.01
    assert node._get_hand_controller_transform("left_hand_root", "pico_left") is None
    assert node._tf_buffer.lookup_transform.call_count == 2


def test_hand_offset_lookup_recovers_and_caches_after_missing_tf(
    node_module, monkeypatch
):
    """A late robot-description TF is discovered without per-frame polling."""
    node = _make_node(node_module)
    now = [100.0]
    monkeypatch.setattr(node_module.time, "monotonic", lambda: now[0])

    tf_msg = node_module.TransformStamped()
    tf_msg.transform.translation.x = 1.0
    tf_msg.transform.translation.y = 2.0
    tf_msg.transform.translation.z = 3.0
    tf_msg.transform.rotation.x = 0.0
    tf_msg.transform.rotation.y = 0.0
    tf_msg.transform.rotation.z = 0.0
    tf_msg.transform.rotation.w = 2.0
    node._tf_buffer.lookup_transform.side_effect = [
        node_module.LookupException("missing"),
        tf_msg,
    ]

    key = ("left_hand_root", "pico_left")
    assert node._get_hand_controller_transform(*key) is None
    now[0] += node_module._TF_OFFSET_RETRY_S + 0.01
    assert node._get_hand_controller_transform(*key) == (
        (1.0, 2.0, 3.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    assert node._get_hand_controller_transform(*key) == (
        (1.0, 2.0, 3.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    assert node._tf_buffer.lookup_transform.call_count == 2
    assert key not in node._hand_controller_retry_after


# ---------------------------------------------------------------------------
# Per-controller sensor_msgs/Joy publishing
# ---------------------------------------------------------------------------


def test_publish_joy_emits_one_per_controller(node_module):
    node = _make_node(node_module)
    node._transport._connected = True
    frame = _make_frame(node_module)

    node._publish_joy_from_data(frame.raw, stamp="STAMP")

    assert len(node.published["/pico/controller/left/joy"]) == 1
    assert len(node.published["/pico/controller/right/joy"]) == 1


def test_publish_joy_left_axes_and_buttons(node_module):
    """Left Joy carries left thumbstick, left triggers/buttons in fixed order."""
    node = _make_node(node_module)
    node._transport._connected = True
    frame = _make_frame(node_module)

    node._publish_joy_from_data(frame.raw, stamp="STAMP")
    left = node.published["/pico/controller/left/joy"][0]

    assert left.header.stamp == "STAMP"
    assert left.axes[0] == pytest.approx(0.4)  # leftJoystick.x
    assert left.axes[1] == pytest.approx(-0.5)  # leftJoystick.y
    # Analog trigger / grip values are NaN when a payload omits those inputs.
    assert math.isnan(left.axes[2])
    assert math.isnan(left.axes[3])
    # Buttons in the order documented next to _publish_controller_joy:
    # primary=X, secondary=Y, menu=Start, thumb_click, trigger_press,
    # grip_press, primary_touch, secondary_touch, thumb_touch, trigger_touch,
    # thumb_rest_touched.
    assert left.buttons == [
        1,  # buttonXPressed
        0,  # buttonYPressed
        1,  # startPressed (menu)
        1,  # leftThumbstickClick
        1,  # leftTriggerPressed
        1,  # leftGripPressed
        1,  # buttonXTouched
        1,  # buttonYTouched
        1,  # leftThumbstickTouched
        1,  # leftIndexTriggerTouched
        0,  # thumb_rest_touched (always 0 under current protocol)
    ]


def test_publish_joy_right_axes_and_buttons(node_module):
    """Right Joy uses A/B/Back semantics and the right-side raw keys."""
    node = _make_node(node_module)
    node._transport._connected = True
    frame = _make_frame(node_module)

    node._publish_joy_from_data(frame.raw, stamp="STAMP")
    right = node.published["/pico/controller/right/joy"][0]

    assert right.axes[0] == pytest.approx(-0.1)  # rightJoystick.x
    assert right.axes[1] == pytest.approx(0.2)  # rightJoystick.y
    assert math.isnan(right.axes[2])
    assert math.isnan(right.axes[3])
    assert right.buttons == [
        1,  # buttonAPressed
        1,  # buttonBPressed
        0,  # backPressed (menu)
        0,  # rightThumbstickClick
        0,  # rightTriggerPressed
        0,  # rightGripPressed
        0,  # buttonATouched
        1,  # buttonBTouched
        1,  # rightThumbstickTouched
        1,  # rightIndexTriggerTouched
        0,
    ]
