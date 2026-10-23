"""Robot-issued, single-use input leases. No sockets, ROS, or device discovery."""

from dataclasses import replace
import json
import base64
import zlib
import math

from teleop.xr.wire import XRFrameMessage, encode_xr_frame_message, decode_xr_frame_message


INPUT_TTL_NS = 100_000_000
MAX_PENDING_CHALLENGES = 16


class StaleInputLease(ValueError):
    """A structurally valid XR reply that no longer has control authority.

    Dropping this reply cannot move the robot: accept() raises before updating
    the accepted sequence/device timestamp or returning an XR frame.
    """


def make_input_packet(challenge, frame, *, now_ns):
    if not isinstance(challenge, dict) or challenge.get('event') != 'challenge' \
            or type(challenge.get('challenge_id')) is not int:
        raise ValueError('robot challenge required')
    if type(now_ns) is not int or frame.stale \
            or not 0 <= now_ns-frame.received_monotonic_ns < INPUT_TTL_NS:
        raise ValueError('PICO source stale or future')
    return dict(event='xr', challenge_id=challenge['challenge_id'],
                message=json.loads(encode_xr_frame_message(XRFrameMessage(now_ns, frame))))


class InputLease:
    """Bound transit age without comparing host and robot monotonic clocks.

    The host cannot have answered before the challenge was issued. Subtracting
    the host's already elapsed source age from issue time is conservative, even
    if that source frame predates the challenge. Delays only shorten the lease.
    This is an authenticated-process protocol, not protection from a hostile PC.
    """

    def __init__(self):
        self._pending = {}
        self._next_id = 0
        self._sequence = -1
        self._device_stamp = 0
        self.last_timing = None

    def challenge(self, now_ns):
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError('robot monotonic timestamp required')
        self._pending = {key: stamp for key, stamp in self._pending.items()
                         if 0 <= now_ns-stamp < INPUT_TTL_NS}
        self._next_id += 1
        self._pending[self._next_id] = now_ns
        # At 100 Hz a 100 ms lease naturally spans ten challenges.  Keeping
        # only eight could evict a still-valid reply during ordinary jitter.
        while len(self._pending) > MAX_PENDING_CHALLENGES:
            del self._pending[min(self._pending)]
        return dict(event='challenge', challenge_id=self._next_id)

    def accept(self, packet, *, now_ns):
        if not isinstance(packet, dict) or set(packet) != {'event', 'challenge_id', 'message'} \
                or packet['event'] != 'xr' or type(packet['challenge_id']) is not int:
            raise ValueError('invalid input envelope')
        self.last_timing = None
        issued = self._pending.pop(packet['challenge_id'], None)
        if issued is None:
            raise StaleInputLease('unknown, expired or reused challenge')
        message = decode_xr_frame_message(json.dumps(packet['message'], allow_nan=False))
        frame = message.frame
        age = message.emitted_monotonic_ns-frame.received_monotonic_ns
        source_ns = issued-age
        if type(now_ns) is not int:
            raise StaleInputLease('robot monotonic timestamp required')
        self.last_timing = dict(challenge_id=packet['challenge_id'],
            challenge_issued_ns=issued, robot_consumed_ns=now_ns,
            pc_source_age_ns=age, conservative_age_ns=now_ns-source_ns)
        if type(now_ns) is not int or frame.stale or age < 0 or source_ns < 0 \
                or now_ns < issued or not 0 <= now_ns-source_ns < INPUT_TTL_NS:
            raise StaleInputLease('input lease expired, stale or future')
        if frame.sequence_id <= self._sequence or frame.device_timestamp_ns < self._device_stamp:
            raise StaleInputLease('input sequence or device timestamp moved backwards')
        self._sequence = frame.sequence_id
        if frame.device_timestamp_ns == self._device_stamp:
            return None
        for pose in (frame.head_pose, frame.left_controller.pose, frame.right_controller.pose):
            norm = sum(v*v for v in pose.orientation_xyzw)
            if not math.isfinite(norm) or not .9 <= norm <= 1.1:
                raise ValueError('PICO pose quaternion invalid; tracking unavailable')
        self._device_stamp = frame.device_timestamp_ns
        return replace(frame, received_monotonic_ns=source_ns)


class ReturnCommandLease(InputLease):
    """Explicit one-shot command and heartbeat, with the same 100 ms lease.

    No PICO data is accepted. A neutral internal frame only feeds the existing
    controllers' freshness gates; its poses never authorize normal arm/trigger
    control. All buttons, grips and triggers remain released.
    """
    def __init__(self):
        super().__init__()
        self.requested = False
        self.request_now = False

    def accept(self, packet, *, now_ns):
        from teleop.contracts.xr import Pose, ControllerState, ControllerButtons, XRFrame
        self.request_now = False
        if not isinstance(packet, dict) or set(packet) != {'event', 'challenge_id', 'action'} \
                or packet['event'] != 'return_command' or type(packet['challenge_id']) is not int \
                or packet['action'] not in ('request', 'heartbeat'):
            raise ValueError('standalone return accepts only request/heartbeat commands')
        issued = self._pending.get(packet['challenge_id'])
        if issued is None or type(now_ns) is not int or not 0 <= now_ns-issued < INPUT_TTL_NS:
            raise StaleInputLease('standalone return challenge expired or reused')
        # Reject older replies before they can become a new input timestamp.
        if packet['challenge_id'] <= self._sequence:
            raise StaleInputLease('standalone return challenge moved backwards')
        if packet['action'] == 'request' and self.requested:
            raise ValueError('standalone return already requested; no automatic retry')
        self._pending.pop(packet['challenge_id'])
        self._sequence = packet['challenge_id']
        self.last_timing = dict(challenge_id=self._sequence, challenge_issued_ns=issued,
                                robot_consumed_ns=now_ns, conservative_age_ns=now_ns-issued)
        self.request_now = packet['action'] == 'request'
        self.requested |= self.request_now
        pose = Pose((0., 0., 0.), (0., 0., 0., 1.))
        neutral = ControllerState(pose, (0., 0.), 0., 0., ControllerButtons())
        return XRFrame(self._sequence, self._sequence, issued, False, pose, neutral, neutral,
                       source='standalone-return-command')


# Only evidence packets may be compressed. Never hide control events inside a
# diagnostic envelope, and cap decompression before allocating an unbounded body.
DIAGNOSTIC_EVENTS = frozenset(('ik_trace', 'ik_failure', 'control_trace', 'input_drop', 'guard_trace', 'reference_not_sent', 'gripper_trace'))
MAX_ROBOT_PACKET_BYTES = 65536


def encode_robot_packet(packet, *, compress_diagnostics=False):
    plain = json.dumps(packet, allow_nan=False, separators=(',', ':'))
    raw = plain.encode('utf-8')
    if len(raw) > MAX_ROBOT_PACKET_BYTES:
        raise ValueError('robot response too large')
    if compress_diagnostics and packet.get('event') in DIAGNOSTIC_EVENTS and len(raw) >= 512:
        data = base64.b64encode(zlib.compress(raw, level=1)).decode('ascii')
        packed = json.dumps(dict(event='diagnostic_zlib', data=data), separators=(',', ':'))
        if len(packed) < len(raw):
            return packed+'\n'
    return plain+'\n'


def decode_robot_packet(line):
    if len(line.encode('utf-8')) > MAX_ROBOT_PACKET_BYTES+1:
        raise ValueError('robot response too large')
    packet = json.loads(line)
    if not isinstance(packet, dict):
        raise ValueError('robot response must be an object')
    if packet.get('event') != 'diagnostic_zlib':
        return packet
    if set(packet) != {'event', 'data'} or not isinstance(packet['data'], str):
        raise ValueError('invalid diagnostic envelope')
    try:
        data = base64.b64decode(packet['data'], validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(data, MAX_ROBOT_PACKET_BYTES+1)
        if len(raw) > MAX_ROBOT_PACKET_BYTES or not inflater.eof or inflater.unused_data:
            raise ValueError('invalid or oversized compressed diagnostic')
        packet = json.loads(raw)
    except (ValueError, zlib.error) as exc:
        raise ValueError('invalid compressed diagnostic: '+str(exc)) from exc
    if not isinstance(packet, dict) or packet.get('event') not in DIAGNOSTIC_EVENTS:
        raise ValueError('compressed control packets are forbidden')
    return packet
