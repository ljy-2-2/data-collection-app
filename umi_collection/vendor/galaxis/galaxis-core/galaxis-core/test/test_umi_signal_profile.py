"""The App's persisted 23-signal contract, independent of live ROS control."""

import json
import struct
import sys
from types import SimpleNamespace

import pytest

h5py = pytest.importorskip("h5py")
from galaxis_core import hdf5_recorder as recorder


class Cdr:
    def __init__(self, endian="<"):
        self.endian = endian
        self.data = bytearray(b"\x00\x01\x00\x00" if endian == "<" else b"\x00\x00\x00\x00")

    def pack(self, code, alignment, value):
        self.data.extend(b"\0" * ((-(len(self.data) - 4)) % alignment))
        self.data.extend(struct.pack(self.endian + code, value))

    def string(self, value):
        value = value.encode() + b"\0"
        self.pack("I", 4, len(value))
        self.data.extend(value)

    def header(self):
        self.pack("i", 4, 123)
        self.pack("I", 4, 456)
        self.string("pico_origin")


def pose_payload(controller=False, shift=0, endian="<"):
    cdr = Cdr(endian)
    cdr.header()
    cdr.pack("Q", 8, 1001)
    cdr.pack("Q", 8, 2002)
    cdr.pack("I", 4, 3)
    cdr.string("test-pico-id")
    for value in (1 + shift, 2, 3, 0, 0, 0, 1):
        cdr.pack("d", 8, value)
    if controller:
        cdr.pack("B", 1, 1)
        cdr.pack("B", 1, 1)
    return bytes(cdr.data)


def joint_payload(side="left", shift=0, endian="<", name=None):
    cdr = Cdr(endian)
    cdr.header()
    # Extra joints/velocity/effort must not leak into the selected recording.
    cdr.pack("I", 4, 2)
    cdr.string(name or f"{side}_right_finger_joint")
    cdr.string("unwanted_joint")
    for values in ((0.25 + shift, 99), (88, 77), (66, 55)):
        cdr.pack("I", 4, len(values))
        for value in values:
            cdr.pack("d", 8, value)
    return bytes(cdr.data)


def offset_payload(value):
    cdr = Cdr()
    cdr.pack("d", 8, value)
    return bytes(cdr.data)


def install_bag(monkeypatch, topics, rows):
    class Reader:
        def open(self, *_):
            self.index = 0

        def get_all_topics_and_types(self):
            return [SimpleNamespace(name=name, type=kind, serialization_format="cdr",
                                    offered_qos_profiles="") for name, kind in topics.items()]

        def has_next(self):
            return self.index < len(rows)

        def read_next(self):
            row = rows[self.index]
            self.index += 1
            return row

    monkeypatch.setitem(sys.modules, "rosbag2_py", SimpleNamespace(
        SequentialReader=Reader, StorageOptions=lambda **kw: kw,
        ConverterOptions=lambda **kw: kw,
    ))


def source_bag(monkeypatch, sides=("left", "right")):
    topics = {"/pico/hmd/state": recorder.UMI_POSE_TOPICS["/pico/hmd/state"]}
    for side in sides:
        topics[f"/pico/controller/{side}/state"] = "galaxis_pico_msgs/msg/PicoController"
        topics[f"/galaxis/gripper/{side}/joint_state"] = "sensor_msgs/msg/JointState"
        topics[f"/galaxis/gripper/{side}/encoder_offset"] = "std_msgs/msg/Float64"
        topics[f"/pico/controller/{side}/joy"] = "sensor_msgs/msg/Joy"
    topics["/joint_states"] = "sensor_msgs/msg/JointState"
    topics["/tf"] = "tf2_msgs/msg/TFMessage"
    topics["/pico/hmd/battery"] = "sensor_msgs/msg/BatteryState"
    rows = []
    for topic in topics:
        if topic in recorder.UMI_OFFSET_TOPICS:
            rows.append((topic, offset_payload(1.25), 90))
    for frame in range(3):
        for topic in topics:
            if topic in recorder.UMI_OFFSET_TOPICS:
                continue
            if topic in recorder.UMI_POSE_TOPICS:
                payload = pose_payload("/controller/" in topic, frame)
            elif topic in recorder.UMI_GRIPPER_TOPICS:
                payload = joint_payload("left" if "/left/" in topic else "right", frame)
            else:
                payload = b"DROPPED-RAW-DATA-MUST-NOT-BE-STORED"
            rows.append((topic, payload, 100 + frame))
    install_bag(monkeypatch, topics, rows)
    return topics, rows


@pytest.mark.parametrize("endian", ["<", ">"])
def test_pose_projection_preserves_header_and_pose_only(endian):
    payload = pose_payload(True, endian=endian)
    projected = recorder._project_umi_payload("/pico/controller/left/state", payload)
    reader = recorder._CdrNumericReader(projected)
    assert reader.i32() == 123
    assert reader.u32() == 456
    assert reader.string() == "pico_origin"
    assert [reader.f64() for _ in range(7)] == [1, 2, 3, 0, 0, 0, 1]
    assert reader.pos == len(projected)  # no flags, buttons, or raw Pico tail
    signals = recorder._numeric_signals("/pico/controller/left/state", recorder.POSE_STAMPED_TYPE, projected)
    assert len(signals) == 7
    assert signals["left_wrist.position.x"] == 1


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("endian", ["<", ">"])
def test_joint_projection_keeps_only_one_position(side, endian):
    topic = f"/galaxis/gripper/{side}/joint_state"
    projected = recorder._project_umi_payload(topic, joint_payload(side, endian=endian))
    reader = recorder._CdrNumericReader(projected)
    reader.header()
    assert reader.strings() == [f"{side}_right_finger_joint"]
    assert reader.floats64() == [0.25]
    assert reader.floats64() == []
    assert reader.floats64() == []
    assert reader.pos == len(projected)


@pytest.mark.parametrize("sides,expected_count", [(("left", "right"), 23), (("left",), 15), (("right",), 15)])
def test_conversion_keeps_only_selected_curves_cdr_and_calibration(monkeypatch, tmp_path, sides, expected_count):
    source_bag(monkeypatch, sides)
    output = tmp_path / "episode.h5"
    result = recorder.convert_rosbag_compact(tmp_path / "bag", output, signal_profile=recorder.UMI_SIGNAL_PROFILE)
    assert result["signal_profile"] == recorder.UMI_SIGNAL_PROFILE
    with h5py.File(output, "r") as handle:
        signals = {str(g.attrs["key"]): g for g in handle["signals"].values()}
        assert len(signals) == expected_count
        assert signals["head.position.x"]["values"][:].tolist() == [1, 2, 3]
        assert signals["head.position.x"]["timestamps_ns"][:].tolist() == [100, 101, 102]
        for side in sides:
            key = f"galaxis.gripper.{side}.joint_state.{side}_right_finger_joint.position"
            assert signals[key]["values"][:].tolist() == [0.25, 1.25, 2.25]
        assert not any("button" in key or "tracked" in key or "encoder_offset" in key or "observation.state" in key for key in signals)
        assert dict(handle["calibration"].attrs) == {f"{side}_encoder_offset_rad": 1.25 for side in sides}
        assert b"DROPPED-RAW-DATA" not in bytes(handle["cdr/data"][:])
        stored_topics = {g.attrs["name"]: g.attrs["type"] for g in handle["topics"].values()}
        assert "/joint_states" not in stored_topics
        assert stored_topics["/pico/hmd/state"] == recorder.POSE_STAMPED_TYPE
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["signal_count"] == expected_count
    assert manifest["signal_profile"] == recorder.UMI_SIGNAL_PROFILE
    assert manifest["artifacts"][0]["sha256"] == recorder._sha256_file(output)
    recorder.verify_existing_rosbag(tmp_path / "bag", output)


@pytest.mark.parametrize("damage", ["signal", "cdr", "calibration"])
def test_validation_detects_corruption(monkeypatch, tmp_path, damage):
    source_bag(monkeypatch)
    output = tmp_path / "episode.h5"
    recorder.convert_rosbag_compact(tmp_path / "bag", output, signal_profile=recorder.UMI_SIGNAL_PROFILE)
    with h5py.File(output, "r+") as handle:
        if damage == "signal":
            handle["signals/0000/values"][0] = 999
        elif damage == "cdr":
            handle["cdr/data"][0] = 255
        else:
            handle["calibration"].attrs["left_encoder_offset_rad"] = 999
    with pytest.raises(ValueError, match="differs|differ"):
        recorder.validate_compact_hdf5(tmp_path / "bag", output)


def test_invalid_required_data_never_publishes_complete_bundle(monkeypatch, tmp_path):
    topics, rows = source_bag(monkeypatch)
    rows.append(("/galaxis/gripper/left/joint_state", joint_payload(name="wrong_joint"), 200))
    output = tmp_path / "episode.h5"
    with pytest.raises(ValueError, match="missing or ambiguous"):
        recorder.convert_rosbag_compact(tmp_path / "bag", output, signal_profile=recorder.UMI_SIGNAL_PROFILE)
    assert not output.exists()
    assert not (tmp_path / "manifest.json").exists()


def test_calibration_change_rejected():
    calibration = {}
    topic = "/galaxis/gripper/left/encoder_offset"
    recorder._capture_calibration(topic, offset_payload(1), calibration)
    recorder._capture_calibration(topic, offset_payload(1), calibration)
    with pytest.raises(ValueError, match="changed"):
        recorder._capture_calibration(topic, offset_payload(2), calibration)


def test_legacy_full_profile_retains_original_payload_and_verifies(monkeypatch, tmp_path):
    topic = "/pico/controller/left/state"
    payload = pose_payload(True)
    install_bag(monkeypatch, {topic: "galaxis_pico_msgs/msg/PicoController"}, [(topic, payload, 100)])
    output = tmp_path / "legacy.h5"
    recorder.convert_rosbag_compact(tmp_path / "bag", output)
    with h5py.File(output, "r+") as handle:
        assert bytes(handle["cdr/data"][:]) == payload
        assert len(handle["signals"]) == 9
        # Existing pre-profile files have neither field; keep recovery compatible.
        del handle.attrs["signal_profile"]
        del handle["calibration"]
    recorder.verify_existing_rosbag(tmp_path / "bag", output)
