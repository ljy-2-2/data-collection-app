"""Tests for the self-contained Galaxis PICO TCP transport."""

from __future__ import annotations

import json
import math

import pytest

from galaxis_pico_bridge.transport.galaxis_tcp import (
    GalaxisPacketDecoder,
    GalaxisTcpServerTransport,
    _encode_server_packet,
    normalize_tracking_payload,
)


def _packet(cmd: int, payload: bytes) -> bytes:
    return (
        b"\x3f"
        + bytes((cmd,))
        + len(payload).to_bytes(4, "little")
        + payload
        # Low byte is deliberately 0xA5, the same as the packet terminator.
        + (1_800_000_000_165).to_bytes(8, "little", signed=True)
        + b"\xa5"
    )


def _tracking() -> dict:
    return {
        "timeStampNs": 987654321,
        "Head": {"pose": "1,2,3,0,0,0,1", "status": 3},
        "Controller": {
            "left": {
                "pose": "0.1,0.2,0.3,0,0,0,1",
                "axisX": 0.25,
                "axisY": -0.5,
                "axisClick": True,
                "grip": 0.75,
                "trigger": 0.125,
                "primaryButton": True,
                "secondaryButton": False,
                "menuButton": True,
            },
            "right": {
                "pose": "-0.1,0.4,0.8,0,0.7071068,0,0.7071068",
                "axisX": -0.2,
                "axisY": 0.4,
                "axisClick": False,
                "grip": 0.1,
                "trigger": 0.9,
                "primaryButton": False,
                "secondaryButton": True,
                "menuButton": False,
            },
        },
    }


def _tracking_envelope(tracking=None) -> bytes:
    return json.dumps(
        {"functionName": "Tracking", "value": json.dumps(tracking or _tracking())},
        separators=(",", ":"),
    ).encode()


def _tracking_at(device_time_ns, left_pose=None) -> bytes:
    tracking = _tracking()
    tracking["timeStampNs"] = device_time_ns
    if left_pose is not None:
        tracking["Controller"]["left"]["pose"] = left_pose
    return _tracking_envelope(tracking)


def test_decoder_accepts_current_galaxis_framing():
    decoder = GalaxisPacketDecoder()
    wire = _packet(0x6D, _tracking_envelope())

    assert decoder.feed(wire[:11]) == []
    packets = decoder.feed(wire[11:])

    assert packets == [(0x6D, _tracking_envelope(), 1_800_000_000_165)]


def test_decoder_handles_coalesced_handshake_and_tracking_packets():
    connect = _packet(0x19, b"PICO4U-001|-1")
    tracking = _packet(0x6D, _tracking_envelope())

    packets = GalaxisPacketDecoder().feed(b"noise" + connect + tracking)

    assert [packet[0] for packet in packets] == [0x19, 0x6D]
    assert packets[0][1] == b"PICO4U-001|-1"


def test_decoder_rejects_removed_zero_length_legacy_framing():
    payload = _tracking_envelope()
    wire = (
        b"\x3f\x6d\x00\x00\x00\x00"
        + payload
        + (1_800_000_000_165).to_bytes(8, "little", signed=True)
        + b"\xa5"
    )

    assert GalaxisPacketDecoder().feed(wire) == []


def test_normalize_tracking_payload_maps_pose_analogs_and_buttons():
    raw = normalize_tracking_payload(_tracking())

    assert raw["deviceTimeNs"] == 987654321
    assert raw["hmdPosition"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert raw["leftControllerPosition"]["x"] == pytest.approx(0.1)
    assert raw["rightControllerRotation"]["w"] == pytest.approx(0.7071068)
    assert raw["leftTracked"] is True
    assert raw["rightValid"] is True
    assert raw["leftJoystick"] == {"x": 0.25, "y": -0.5}
    assert raw["leftGripValue"] == pytest.approx(0.75)
    assert raw["rightTriggerValue"] == pytest.approx(0.9)
    assert raw["leftGripPressed"] is True
    assert raw["rightTriggerPressed"] is True
    assert raw["buttonXPressed"] is True
    assert raw["buttonBPressed"] is True
    assert raw["startPressed"] is True
    assert raw["backPressed"] is False


def test_normalize_tracking_payload_preserves_full_pose_values_without_scaling():
    payload = _tracking()
    payload["Head"]["pose"] = "-1.25,2.5,3.75,-0.5,0.25,-0.125,0.8196798"
    payload["Controller"]["left"]["pose"] = "0.987,-0.654,0.321,0.1,-0.2,0.3,0.9273618"

    raw = normalize_tracking_payload(payload)

    assert raw["hmdPosition"] == {"x": -1.25, "y": 2.5, "z": 3.75}
    assert raw["hmdRotation"] == {
        "x": -0.5,
        "y": 0.25,
        "z": -0.125,
        "w": 0.8196798,
    }
    assert raw["leftControllerPosition"] == {
        "x": 0.987,
        "y": -0.654,
        "z": 0.321,
    }


def test_server_packet_matches_unity_receive_framing():
    payload = b'{"functionName":"timeTest","value":"123"}'

    wire = _encode_server_packet(0x5F, payload, timestamp_ms=1_800_000_000_165)

    assert wire[:2] == b"\xcf\x5f"
    assert int.from_bytes(wire[2:6], "little") == len(payload)
    assert wire[6 : 6 + len(payload)] == payload
    assert int.from_bytes(wire[-9:-1], "little", signed=True) == 1_800_000_000_165
    assert wire[-1:] == b"\xa5"


def test_transport_emits_protocol_neutral_frame_without_external_sdk():
    received = []
    transport = GalaxisTcpServerTransport(
        host="127.0.0.1", port=0, on_frame=received.append
    )
    transport._handle_packet(0x19, b"PICO4U-001|-1")
    transport._handle_packet(0x6D, _tracking_envelope())

    assert len(received) == 1
    frame = received[0]
    assert frame.device_time_ns == 987654321
    assert frame.seq == 1
    assert frame.pico_id == "PICO4U-001"
    assert frame.pc_monotonic_ns > 0
    assert math.isnan(frame.delta_time_s)


def test_transport_rejects_packets_from_superseded_connection_generation():
    received = []
    transport = GalaxisTcpServerTransport(
        host="127.0.0.1", port=0, on_frame=received.append
    )
    transport._connection_generation = 8

    transport._handle_packet(
        0x6D,
        _tracking_at(2_000, "0.8,0.7,0.6,0,0,0,1"),
        generation=7,
    )
    transport._handle_packet(
        0x6D,
        _tracking_at(3_000, "0.3,0.2,0.1,0,0,0,1"),
        generation=8,
    )

    assert len(received) == 1
    assert received[0].device_time_ns == 3_000
    assert received[0].raw["leftControllerPosition"] == {
        "x": 0.3,
        "y": 0.2,
        "z": 0.1,
    }
    assert received[0].seq == 1


def test_transport_drops_regressing_timestamp_within_current_generation():
    received = []
    transport = GalaxisTcpServerTransport(
        host="127.0.0.1", port=0, on_frame=received.append
    )
    transport._connection_generation = 4

    transport._handle_packet(0x6D, _tracking_at(2_000), generation=4)
    transport._handle_packet(0x6D, _tracking_at(1_000), generation=4)
    transport._handle_packet(0x6D, _tracking_at(2_500), generation=4)

    assert [frame.device_time_ns for frame in received] == [2_000, 2_500]
    assert [frame.seq for frame in received] == [1, 2]
    assert received[1].delta_time_s == pytest.approx(0.0000005)
    assert transport._stale_frame_drops == 1


def test_transport_accepts_unity_time_test_reply_without_treating_it_as_json():
    transport = GalaxisTcpServerTransport(
        host="127.0.0.1", port=0, on_frame=lambda _frame: None
    )

    transport._handle_packet(0x6D, b"timeTest")


def test_transport_sends_bounded_liveness_packet_to_headset():
    sent = []

    class FakeSocket:
        def sendall(self, data):
            sent.append(data)

    transport = GalaxisTcpServerTransport(
        host="127.0.0.1", port=0, on_frame=lambda _frame: None
    )
    transport._send_liveness_probe(FakeSocket())

    assert len(sent) == 1
    assert sent[0][:2] == b"\xcf\x5f"
    payload_length = int.from_bytes(sent[0][2:6], "little")
    payload = json.loads(sent[0][6 : 6 + payload_length])
    assert payload["functionName"] == "timeTest"


def test_transport_receives_first_frame_before_sending_liveness():
    received = []

    class FakeSocket:
        def __init__(self):
            self.recv_count = 0
            self.sent = []

        def recv(self, _size):
            self.recv_count += 1
            if self.recv_count == 1:
                return _packet(
                    0x6D, _tracking_envelope()
                )
            return b""

        def sendall(self, data):
            self.sent.append(data)

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    transport = GalaxisTcpServerTransport(
        host="127.0.0.1",
        port=0,
        on_frame=received.append,
        liveness_interval_sec=5.0,
    )
    client = FakeSocket()
    transport._running = True
    transport._client = client
    transport._connected = True
    transport._connection_generation = 1

    transport._recv_loop(client, generation=1)

    assert len(received) == 1
    assert client.sent == []


def test_invalid_pose_is_not_marked_tracked():
    payload = _tracking()
    payload["Controller"]["left"]["pose"] = "not-a-pose"

    raw = normalize_tracking_payload(payload)

    assert raw["leftTracked"] is False
    assert raw["leftValid"] is False
    assert "leftControllerPosition" not in raw


def test_slow_liveness_send_does_not_block_inbound_tracking():
    import socket
    import threading
    import time
    frames = []
    entered, release = threading.Event(), threading.Event()
    transport = GalaxisTcpServerTransport(host='127.0.0.1', port=0,
        on_frame=frames.append, liveness_interval_sec=.5)
    def slow_probe(_):
        entered.set()
        assert release.wait(3)
    transport._send_liveness_probe = slow_probe
    transport.start_listener()
    client = socket.create_connection(transport._listener.getsockname())
    try:
        assert entered.wait(2)
        client.sendall(_packet(0x6D, _tracking_envelope()))
        end = time.monotonic() + 1
        while not frames and time.monotonic() < end:
            time.sleep(.005)
        # The heartbeat is still blocked, yet tracking has been delivered.
        assert len(frames) == 1
        assert not release.is_set()
    finally:
        release.set()
        client.close()
        transport.stop()


def test_liveness_failure_does_not_disconnect_healthy_inbound_stream():
    import socket
    import threading
    transport = GalaxisTcpServerTransport(host='127.0.0.1', port=0, on_frame=lambda _: None)
    transport._running = transport._connected = True
    transport._connection_generation = 1
    client = object()
    transport._client = client
    def fail(_):
        raise socket.timeout('send timed out')
    transport._send_liveness_probe = fail
    transport._liveness_interval_sec = .001
    transport._probe_loop(client, 1, threading.Event())
    assert transport.connected
    assert 'timed out' in transport.metrics()['liveness_send_error']
