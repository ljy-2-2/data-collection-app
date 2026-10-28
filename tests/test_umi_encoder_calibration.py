"""Exercise the real encoder reader and published recording values without ROS."""

import importlib.util
import math
from pathlib import Path
import struct
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest
import yaml


GALAXIS = Path(__file__).resolve().parents[1] / "umi_collection/vendor/galaxis"
BRINGUP = GALAXIS / "galaxis_bringup/galaxis_bringup"


def load_module(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class JointState:
    def __init__(self):
        self.header = SimpleNamespace(stamp=None, frame_id="")
        self.name = []
        self.position = []
        self.velocity = []
        self.effort = []


class Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


@pytest.fixture
def encoder_factory(monkeypatch):
    # Only ROS infrastructure is replaced; parsing, offset/direction, unwrap,
    # reconnect reset and JointState publishing all execute production code.
    for name, module in {
        "serial": SimpleNamespace(Serial=object, SerialException=OSError),
        "rclpy": ModuleType("rclpy"),
        "rclpy.node": SimpleNamespace(Node=object),
        "rclpy.qos": SimpleNamespace(QoSProfile=object, DurabilityPolicy=object),
        "sensor_msgs": ModuleType("sensor_msgs"),
        "sensor_msgs.msg": SimpleNamespace(JointState=JointState),
        "std_msgs": ModuleType("std_msgs"),
        "std_msgs.msg": SimpleNamespace(Float64=SimpleNamespace, String=SimpleNamespace),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    package = ModuleType("_calibration_test_bringup")
    package.__path__ = [str(BRINGUP)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    load_module(monkeypatch, f"{package.__name__}.encoder_protocol", BRINGUP / "encoder_protocol.py")
    module = load_module(monkeypatch, f"{package.__name__}.encoder_node", BRINGUP / "encoder_node.py")
    parameters = yaml.safe_load((GALAXIS / "galaxis_bringup/config/galaxis_config.yaml").read_text())[
        "galaxis_devices"
    ]["encoder_node"]["ros__parameters"]

    def make(side, closed_degrees):
        node = module.DualEncoderJointStateNode.__new__(module.DualEncoderJointStateNode)
        node.active_sides = (side,)
        node.left_offset = math.radians(closed_degrees) if side == "left" else 0.0
        node.right_offset = math.radians(closed_degrees) if side == "right" else 0.0
        for arm in ("left", "right"):
            setattr(node, f"{arm}_direction", parameters[f"{arm}_rotation_direction"])
            setattr(node, f"{arm}_joint_name", parameters[f"{arm}_joint_name"])
            setattr(node, f"{arm}_state", module.SideState(f"/dev/{arm}", 115200, threading.Lock()))
        node._states = {arm: getattr(node, f"{arm}_state") for arm in ("left", "right")}
        node.frame_id = ""
        node.data_timeout_sec = 10.0
        node._stop = threading.Event()
        node._last_id_data = node._last_left_id = node._last_right_id = None
        node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=123, nanosec=456)))
        logger = SimpleNamespace(info=lambda *_: None, warning=lambda *_: None, debug=lambda *_: None)
        node.get_logger = lambda: logger
        for name in ("pub", "opening_pub", "left_pub", "right_pub", "id_pub", "left_id_pub", "right_id_pub"):
            setattr(node, name, Publisher())
        return node

    return make


def read_and_publish(node, side, absolute_degrees):
    node._stop.clear()
    state = node._states[side]

    def readline():
        node._stop.set()  # Finish the production reader loop after this sample.
        return f"{side[0].upper()}001,{math.radians(absolute_degrees):.17g}\n".encode()

    state.ser = SimpleNamespace(read=lambda _n: readline(), in_waiting=100, close=lambda: None)
    state.connected = True
    node._reader_loop(side, state)
    node.on_timer()
    message = getattr(node, f"{side}_pub").messages[-1]
    assert node.pub.messages[-1].position == message.position
    assert message.name == [getattr(node, f"{side}_joint_name")]
    return message


@pytest.mark.parametrize("side,closed,opened", [("left", 237.0, 222.0), ("right", 237.0, 252.0)])
def test_nonzero_absolute_reading_is_saved_as_calibrated_zero_then_positive_opening(
    encoder_factory, side, closed, opened,
):
    node = encoder_factory(side, closed)
    messages = [read_and_publish(node, side, value) for value in (closed, opened, closed)]
    assert [math.degrees(message.position[0]) for message in messages] == pytest.approx([0, 15, 0])
    # The wire/storage contract remains radians, with degrees only for display.
    assert messages[1].position == pytest.approx([math.pi / 12])


@pytest.mark.parametrize("side,closed,opened", [("left", 2.0, 347.0), ("right", 358.0, 13.0)])
def test_starting_with_open_gripper_keeps_calibrated_zero_instead_of_rezeroing_first_sample(
    encoder_factory, side, closed, opened,
):
    node = encoder_factory(side, closed)
    assert math.degrees(read_and_publish(node, side, opened).position[0]) == pytest.approx(15)
    assert math.degrees(read_and_publish(node, side, closed).position[0]) == pytest.approx(0)


@pytest.mark.parametrize("side,closed,readings", [
    ("left", 2.0, [2, 1, 0, 359, 350, 0, 2]),
    ("right", 358.0, [358, 359, 0, 1, 10, 0, 358]),
])
def test_crossing_absolute_encoder_wrap_has_no_360_degree_jump(encoder_factory, side, closed, readings):
    node = encoder_factory(side, closed)
    values = [math.degrees(read_and_publish(node, side, raw).position[0]) for raw in readings]
    assert values == pytest.approx([0, 1, 2, 3, 12, 2, 0])


@pytest.mark.parametrize("side", ["left", "right"])
def test_negative_noise_around_closed_position_is_published_as_zero(encoder_factory, side):
    node = encoder_factory(side, 100)
    direction = getattr(node, f"{side}_direction")
    noise_samples = [-0.01, 0, 0.01, -0.01, 0]
    values = [math.degrees(read_and_publish(node, side, 100 + direction * noise).position[0])
              for noise in noise_samples]
    assert values == pytest.approx([0, 0, 0.01, 0, 0])
    assert values[0] == values[3] == 0.0


@pytest.mark.parametrize("side,closed", [("left", math.degrees(4.83959554)),
                                        ("right", math.degrees(6.0505715))])
@pytest.mark.parametrize("opening", [0.01, 38.0, 48.0, 60.0, 90.0])
def test_both_grippers_preserve_measured_travel_without_side_specific_gain_or_limit(
    encoder_factory, side, closed, opening,
):
    node = encoder_factory(side, closed)
    direction = getattr(node, f"{side}_direction")
    read_and_publish(node, side, closed)
    message = read_and_publish(node, side, (closed + direction * opening) % 360)
    assert message.position == pytest.approx([math.radians(opening)])


@pytest.mark.parametrize("side,closed", [("left", 359.8), ("right", 0.2)])
def test_repeated_negative_angles_then_opening_has_no_accumulated_drift(encoder_factory, side, closed):
    node = encoder_factory(side, closed)
    direction = getattr(node, f"{side}_direction")
    signed_angles = [-1, -2, -2, -0.5, 0, 1, 5, 0]
    published = []
    for angle in signed_angles:
        raw = (closed + direction * angle) % 360
        published.append(math.degrees(read_and_publish(node, side, raw).position[0]))
        # Clamp only the emitted value; the continuous state must preserve the
        # actual signed angle across both a raw wrap and repeated negative noise.
        assert math.degrees(node._states[side].continuous) == pytest.approx(angle)
    assert published == pytest.approx([0, 0, 0, 0, 0, 1, 5, 0])


@pytest.mark.parametrize("side,closed,opened", [("left", 2.0, 347.0), ("right", 358.0, 13.0)])
def test_reconnect_keeps_original_closed_zero(encoder_factory, side, closed, opened):
    node = encoder_factory(side, closed)
    read_and_publish(node, side, opened)
    node._mark_disconnected(side, node._states[side], "test serial disconnect")
    assert node._get_latest(side, node._states[side]) is None
    values = [math.degrees(read_and_publish(node, side, raw).position[0]) for raw in (opened, closed)]
    assert values == pytest.approx([15, 0])


def joint_cdr(message):
    """Encode the published JointState, preserving its actual configured name."""
    data = bytearray(b"\x00\x01\x00\x00")

    def pack(code, alignment, value):
        data.extend(b"\0" * ((-(len(data) - 4)) % alignment))
        data.extend(struct.pack("<" + code, value))

    def string(value):
        encoded = value.encode() + b"\0"
        pack("I", 4, len(encoded))
        data.extend(encoded)

    pack("i", 4, message.header.stamp.sec)
    pack("I", 4, message.header.stamp.nanosec)
    string(message.header.frame_id)
    pack("I", 4, len(message.name))
    for name in message.name:
        string(name)
    for values in (message.position, message.velocity, message.effort):
        pack("I", 4, len(values))
        for value in values:
            pack("d", 8, value)
    return bytes(data)


def decode_joint_position(payload):
    """Read the position back from persisted standard JointState CDR."""
    offset = 12  # CDR encapsulation and Header.stamp.

    def read_u32():
        nonlocal offset
        offset += (-(offset - 4)) % 4
        value = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        return value

    size = read_u32()
    offset += size  # Header.frame_id.
    for _ in range(read_u32()):
        size = read_u32()
        offset += size  # Joint name.
    assert read_u32() == 1
    offset += (-(offset - 4)) % 8
    return struct.unpack_from("<d", payload, offset)[0]


@pytest.mark.parametrize("side,closed,opened", [("left", 237.0, 222.0), ("right", 237.0, 252.0)])
def test_mcap_packaging_preserves_actual_published_relative_angles(
    encoder_factory, monkeypatch, tmp_path, side, closed, opened,
):
    mcap_writer = pytest.importorskip("mcap.writer")
    mcap_reader = pytest.importorskip("mcap.reader")
    bundle = load_module(monkeypatch, "_calibration_test_video_bundle",
                         GALAXIS / "galaxis-core/galaxis-core/galaxis_core/video_bundle.py")
    bridge_path = GALAXIS / "monitor/bridge/galaxis_studio_bridge"
    package = ModuleType("_calibration_test_monitor")
    package.__path__ = [str(bridge_path)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    load_module(monkeypatch, f"{package.__name__}.config", bridge_path / "config.py")
    monitor = load_module(monkeypatch, f"{package.__name__}.ros_state", bridge_path / "ros_state.py")
    node = encoder_factory(side, closed)
    direction = getattr(node, f"{side}_direction")
    absolute_samples = (
        closed - direction * 0.25, closed,
        closed + direction * 0.0049, closed + direction * 0.0051,
        closed + direction * 0.0151, opened, closed - direction * 0.02, closed,
    )
    messages = [read_and_publish(node, side, value) for value in absolute_samples]
    assert [math.degrees(message.position[0]) for message in messages] == pytest.approx(
        [0, 0, 0.0049, 0.0051, 0.0151, 15, 0, 0])
    assert messages[0].position == messages[6].position == [0.0]
    monitor_positions = [monitor._parse_joint_state(message)["position"] for message in messages]
    assert monitor_positions == [message.position[0] for message in messages]
    payloads = [joint_cdr(message) for message in messages]
    source = tmp_path / "source.mcap"
    with source.open("wb") as stream:
        writer = mcap_writer.Writer(stream, compression=mcap_writer.CompressionType.NONE)
        writer.start(profile="ros2")
        schema = writer.register_schema("sensor_msgs/msg/JointState", "ros2msg", b"JointState")
        channel = writer.register_channel(f"/galaxis/gripper/{side}/joint_state", "cdr", schema)
        for sequence, payload in enumerate(payloads):
            writer.add_message(channel, 1000 + sequence, payload, 900 + sequence, sequence)
        writer.finish()

    # Exercise the exact non-image copy path used by MP4 bundle conversion.
    destination = tmp_path / "signals.mcap"
    with source.open("rb") as stream, destination.open("wb") as output:
        writer = mcap_writer.Writer(output, compression=mcap_writer.CompressionType.NONE)
        writer.start(profile="ros2")
        schemas, channels = {}, {}
        for schema, channel, message in mcap_reader.make_reader(stream).iter_messages():
            bundle._copy_message(writer, schemas, channels, schema, channel, message)
        writer.finish()
    with destination.open("rb") as stream:
        stored = [message for _, _, message in mcap_reader.make_reader(stream).iter_messages()]
    assert [message.data for message in stored] == payloads
    assert [message.log_time for message in stored] == list(range(1000, 1000 + len(messages)))
    assert [message.publish_time for message in stored] == list(range(900, 900 + len(messages)))
    assert bundle._signal_digest(source) == bundle._signal_digest(destination)
    saved_positions = [decode_joint_position(message.data) for message in stored]
    assert saved_positions == monitor_positions
    assert [f"{math.degrees(value):.2f}" for value in saved_positions] == [
        f"{math.degrees(value):.2f}" for value in monitor_positions
    ] == ["0.00", "0.00", "0.00", "0.01", "0.02", "15.00", "0.00", "0.00"]


def test_wrong_side_encoder_never_publishes_as_selected_hand(encoder_factory):
    node=encoder_factory('left',0)
    state=node._states['left']
    def readline():
        node._stop.set()
        return b'R003,0.2\n'
    state.ser=SimpleNamespace(read=lambda _n: readline(),in_waiting=100,close=lambda:None)
    state.connected=True
    node._reader_loop('left',state)
    node.on_timer()
    assert not node.left_pub.messages and not node.pub.messages
    assert not state.connected


def test_stale_encoder_not_republished_with_fresh_timestamp(encoder_factory,monkeypatch):
    node=encoder_factory('left',0)
    read_and_publish(node,'left',2)
    node.data_timeout_sec=.05
    state=node._states['left']
    import time
    monkeypatch.setattr(time,'monotonic',lambda:state.last_update_time+.06)
    before=len(node.pub.messages)
    node.on_timer()
    assert len(node.pub.messages)==before


@pytest.mark.parametrize("side,closed,opened", [("left", 237, 222), ("right", 237, 252)])
def test_total_opening_is_twice_joint_angle_with_same_timestamp(encoder_factory, side, closed, opened):
    node = encoder_factory(side, closed)
    for value in (closed, opened, closed):
        joint = read_and_publish(node, side, value)
        opening = node.opening_pub.messages[-1]
        assert opening.name == [f"{side}_opening_angle"]
        assert opening.header.stamp == joint.header.stamp
        assert opening.position == pytest.approx([2 * joint.position[0]])
    assert node.opening_pub.messages[1].position == pytest.approx([math.pi / 6])
    # No fake samples are emitted when the physical encoder is unavailable.
    count = len(node.opening_pub.messages)
    node._states[side].last_update_time = None
    node.on_timer()
    assert len(node.opening_pub.messages) == count


@pytest.mark.parametrize("side", ["left", "right"])
def test_opening_channel_mcap_roundtrip(encoder_factory, tmp_path, side):
    writer_module = pytest.importorskip("mcap.writer")
    reader_module = pytest.importorskip("mcap.reader")
    node = encoder_factory(side, 0)
    read_and_publish(node, side, -15 if side == "left" else 15)
    opening = node.opening_pub.messages[-1]
    target = tmp_path / 'opening.mcap'
    with target.open('wb') as stream:
        writer = writer_module.Writer(stream)
        writer.start(profile='ros2')
        schema = writer.register_schema('sensor_msgs/msg/JointState', 'ros2msg', b'JointState')
        channel = writer.register_channel('/galaxis/gripper/opening_state', 'cdr', schema)
        writer.add_message(channel, 123, joint_cdr(opening), 123)
        writer.finish()
    with target.open('rb') as stream:
        _, channel, message = next(reader_module.make_reader(stream).iter_messages())
    assert channel.topic == '/galaxis/gripper/opening_state'
    assert decode_joint_position(message.data) == pytest.approx(math.pi / 6)
