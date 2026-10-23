"""Explicit single-owner gripper sessions; construction performs no I/O.

Only start() connects to already-running local Leju owner sockets. It reads and
verifies configuration; it never changes mode, watchdog or enables the motors.
Robot loop submit() is bounded/nonblocking, with one pending batch per side and
priority revocation. Serial/CAN ownership remains in the native robot owner.
"""
import math
import select
import struct
import threading
import time

from .damiao_gripper import DamiaoGripperDriver, DISABLE, ENABLE, encode_position
from .kuavo5_gripper_live import OwnerBusy, OwnerExpired, OwnerClient, inspect_motor


def limit_wire_target(profile, requested, previous, elapsed_ns):
    """Bound a NEW target relative to the last owner-acknowledged target.

    Queue acceptance is not wire transmission. Use elapsed time since receipt
    of the last successful owner ACK (a conservative upper bound on its send
    time), never input sampling time or time accumulated by rejected writes.
    Leave gains/velocity/feedforward/current fields untouched. Native limits
    and original command deadlines remain authoritative.
    """
    if previous is None:
        raise RuntimeError('active target has no acknowledged seed')
    if profile.control_mode == 1:
        origin = int.from_bytes(previous[:2], 'big')
        wanted = int.from_bytes(requested[:2], 'big')
        ticks = math.floor(profile.speed_rad_s*max(0, elapsed_ns)/1e9 /
                           (2*profile.pmax/65535))
        actual = max(origin-ticks, min(origin+ticks, wanted))
        return actual.to_bytes(2, 'big')+requested[2:]
    origin = struct.unpack('<f', previous[:4])[0]
    wanted = struct.unpack('<f', requested[:4])[0]
    step = profile.speed_rad_s*max(0, elapsed_ns)/1e9
    actual = max(origin-step, min(origin+step, wanted))
    # A recovery seed may be just outside normal travel. Intermediate wire
    # targets must move inward; the native owner independently enforces this.
    if not profile.bounds[0] <= actual <= profile.bounds[1]:
        if not (profile.position_recovery and
                ((origin < profile.bounds[0] and origin <= actual < profile.bounds[0]) or
                 (origin > profile.bounds[1] and profile.bounds[1] < actual <= origin))):
            raise ValueError('recovery target must move inward')
    return encode_position(profile, actual, takeover=True)[:4]+requested[4:]


def wire_position(profile, payload):
    if profile.control_mode == 1:
        return int.from_bytes(payload[:2], 'big')*(2*profile.pmax/65535)-profile.pmax
    return struct.unpack('<f', payload[:4])[0]


class OwnerGripperSession:
    def __init__(self, directory, profiles, occupied_ids, log=None, *, client_factory=OwnerClient):
        self.directory = directory
        self.profiles = dict(profiles)
        self._log_callback = log or (lambda event, **fields: None)
        self._log_lock = threading.Lock()
        self._condition = threading.Condition()
        self._clients = {}
        self._threads = {}
        self._pending = {}
        self._generation = dict.fromkeys(self.profiles, 0)
        self._possibly_enabled = dict.fromkeys(self.profiles, False)
        self._disabled_ack = dict.fromkeys(self.profiles, False)
        self._errors = {}
        self._idle_busy = dict.fromkeys(self.profiles, 0)
        self._idle_expired = dict.fromkeys(self.profiles, 0)
        self._reading = set()
        self._dropped_targets = dict.fromkeys(self.profiles, 0)
        self._wire_payload = dict.fromkeys(self.profiles, None)
        self._wire_ack_ns = dict.fromkeys(self.profiles, 0)
        self._rate_limited_targets = dict.fromkeys(self.profiles, 0)
        self._closing = False
        self._closed = False
        self._started = False
        self._client_factory = client_factory
        self.driver = DamiaoGripperDriver(self.profiles, occupied_ids=occupied_ids, submit=self.submit,
                                         acknowledged_target=self.acknowledged_target)

    def acknowledged_target(self, side):
        with self._condition:
            payload = self._wire_payload[side]
            return ((wire_position(self.profiles[side], payload), self._wire_ack_ns[side])
                    if payload is not None else None)

    def _log(self, event, **fields):
        with self._log_lock:
            self._log_callback(event, **fields)

    def start(self):
        if self._started or self._closed:
            raise RuntimeError("owner session cannot be started twice")
        try:
            for side, p in self.profiles.items():
                client = self._client_factory(self.directory, side,
                    lambda event, side=side, **fields: self._log(event, side=side, **fields))
                self._clients[side] = client
                client.connect()
                if not client.control:
                    raise RuntimeError(side+": owner is inspect-only; explicit control owner required")
                values = inspect_motor(client)
                if values["mode"] != p.control_mode:
                    raise RuntimeError(side+": motor mode differs from commissioned profile; no automatic switch")
                if values["watchdog_ticks"] != 4000:
                    raise RuntimeError(side+": watchdog must read back 4000; run explicit --prepare-watchdog first")
                for key in ("imax_a", "pmax", "vmax", "tmax"):
                    if not math.isclose(values[key], getattr(p, key), rel_tol=1e-5, abs_tol=1e-6):
                        raise RuntimeError(side+": actual "+key+" differs from calibration")
                if (p.bus, p.motor_id, p.master_id) != (client.bus, client.mid, 0):
                    raise RuntimeError(side+": profile differs from native fixed route")
                client.on_feedback = lambda event, side=side: self._feedback(side, event)
                client.refresh_status(expected_state=0)
                origin = client.register(0x50, "f")
                if not p.bounds[0] <= origin <= p.bounds[1]:
                    raise RuntimeError(side+": position outside soft travel; move disabled mechanism inside bounds")
                client.commission(p)
            self._started = True
            for side in self.profiles:
                thread = threading.Thread(target=self._worker, args=(side,),
                                          name="kuavo-gripper-"+side, daemon=True)
                self._threads[side] = thread
                thread.start()
            return self
        except BaseException:
            for client in self._clients.values():
                client.close()
            self._closed = True
            raise

    def _feedback(self, side, event):
        p = self.profiles[side]
        self.driver.receive(p.bus, event["can_id"], event["data"], received_ns=event["received_ns"])

    def submit(self, bus, frames, deadline_ns):
        """Accept all of an ordered batch or none; never block waiting for CAN."""
        side = next((s for s, p in self.profiles.items() if p.bus == bus), None)
        if side is None:
            raise ValueError("unknown commissioned bus")
        p = self.profiles[side]
        if (not isinstance(frames, tuple) or not 1 <= len(frames) <= 2 or
                any(cid != p.command_can_id or not isinstance(data, bytes) or len(data) != 8
                    for cid, data in frames)):
            raise ValueError("invalid motor batch")
        disable = len(frames) == 1 and frames[0][1] == DISABLE
        if type(deadline_ns) is not int or deadline_ns <= time.monotonic_ns():
            return False
        if not self._condition.acquire(blocking=False):
            return False
        try:
            if not self._started or self._closed or self._closing or side in self._errors:
                return False
            if (side in self._pending or side in self._reading) and not disable:
                return False
            if disable:
                self._generation[side] += 1
            self._pending[side] = (frames, deadline_ns, self._generation[side])
            self._condition.notify_all()
            return True
        finally:
            self._condition.release()

    def _worker(self, side):
        client = self._clients[side]
        p = self.profiles[side]
        next_status = 0.
        busy_since = None
        operation = 'idle'
        try:
            while True:
                with self._condition:
                    batch = self._pending.pop(side, None)
                    closing = self._closing
                if batch is not None:
                    frames, deadline, generation = batch
                    for cid, payload in frames:
                        operation = ('enable' if payload == ENABLE else 'disable' if payload == DISABLE
                                     else 'target' if self._possibly_enabled[side] else 'seed')
                        if generation != self._generation[side]:
                            break  # Revocation discards the unsent remainder.
                        if deadline <= time.monotonic_ns():
                            if operation == 'target':
                                self._drop_target(side, 'expired before IPC submission')
                                break
                            raise OwnerExpired(operation+" batch expired before IPC submission")
                        if payload == ENABLE:
                            self._disabled_ack[side] = False  # Previous-cycle ACK is no proof for this enable.
                            self._possibly_enabled[side] = True  # Ambiguous send counts.
                        if operation == 'target':
                            if not self._wait_target_slot(side, deadline, generation):
                                self._drop_target(side, 'expired or revoked while waiting for send slot')
                                break
                            elapsed = time.monotonic_ns()-self._wire_ack_ns[side]
                            requested = payload
                            payload = limit_wire_target(p, requested, self._wire_payload[side], elapsed)
                            if payload != requested:
                                with self._condition:
                                    self._rate_limited_targets[side] += 1
                                self._log('target_rate_limited', side=side,
                                          requested_data_hex=requested.hex(), data_hex=payload.hex(),
                                          elapsed_since_ack_ns=elapsed, deadline_ns=deadline)
                        try:
                            sent = client.send(cid, payload, deadline_ns=deadline)
                        except (OwnerBusy, OwnerExpired) as exc:
                            if operation != 'target':
                                raise
                            # Explicitly unsent position only: discard it. The
                            # next loop may use a NEW input-leased target. Never
                            # extend this target or retry unknown outcomes. The
                            # native 100 ms watchdog remains authoritative.
                            self._drop_target(side, str(exc))
                            break
                        # Only successful native write acknowledgement advances
                        # this baseline. Expired/BUSY/unknown writes never do.
                        if operation in ('seed', 'target'):
                            with self._condition:
                                self._wire_payload[side] = payload
                                self._wire_ack_ns[side] = time.monotonic_ns()
                        if payload in (ENABLE, DISABLE):
                            client.wait_state(1 if payload == ENABLE else 0, sent, .08)
                        if payload == ENABLE:
                            # Native resets its step clock on enable too.
                            self._wire_ack_ns[side] = time.monotonic_ns()
                        if payload == DISABLE:
                            self._possibly_enabled[side] = False
                            self._disabled_ack[side] = True
                            self._wire_payload[side] = None
                            self._log("disabled_acknowledged", side=side)
                    continue
                if closing:
                    if self._possibly_enabled[side]:
                        operation = 'final disable'
                        sent = client.send(p.command_can_id, DISABLE,
                                           deadline_ns=time.monotonic_ns()+50_000_000)
                        client.wait_state(0, sent, .1)
                        self._possibly_enabled[side] = False
                        self._disabled_ack[side] = True
                        self._log("disabled_acknowledged", side=side)
                    break
                if not self._possibly_enabled[side] and time.monotonic() >= next_status:
                    # Status carries no target; exact position supports a seed at
                    # current pose after the operator has released the trigger.
                    with self._condition:
                        if side in self._pending or self._closing:
                            continue
                        self._reading.add(side)
                    operation = 'idle query'
                    try:
                        client.register(0x50, "f")
                        client.refresh_status(expected_state=0)
                    except (OwnerBusy, OwnerExpired) as exc:
                        # Only idle, explicitly unsent reads can be tried again.
                        # Active/enable/disable batches and ambiguous failures
                        # still terminate the worker; no old target is replayed.
                        now = time.monotonic()
                        if busy_since is None:
                            busy_since = now
                        with self._condition:
                            counter = self._idle_busy if isinstance(exc, OwnerBusy) else self._idle_expired
                            counter[side] += 1
                        self._log("idle_read_busy" if isinstance(exc, OwnerBusy) else "idle_read_expired", side=side,
                                  consecutive_busy_seconds=now-busy_since,
                                  rejected_request_can_bytes_sent=False)
                        if now-busy_since >= 1.:
                            raise TimeoutError("idle CAN reads busy for 1 s; no control sent")
                        next_status = now+.007
                        continue
                    finally:
                        with self._condition:
                            self._reading.discard(side)
                    if busy_since is not None:
                        self._log("idle_read_recovered", side=side)
                    busy_since = None
                    next_status = time.monotonic()+.035
                elif select.select([client.sock], [], [], .001)[0]:
                    client.receive(.01)
                else:
                    with self._condition:
                        self._condition.wait(.001)
        except Exception as exc:
            with self._condition:
                self._errors[side] = str(exc)
                self._pending.pop(side, None)
            self._log("owner_worker_failed", side=side, error=str(exc), operation=operation,
                      disable_confirmed=not self._possibly_enabled[side])
        finally:
            # Disconnect causes the native owner to revoke active/seeded state.
            # This is not proof that a disable reached the physical motor.
            client.close()

    def _wait_target_slot(self, side, deadline, generation):
        # Do not immediately drain a queued target after an ACK. Otherwise a
        # uniformly delayed owner leaves <1 position tick of safe progress on
        # every submission, starving motion. Pace at most 100 Hz after ACK;
        # do not hold the queue lock while waiting, extend leases, or delay a
        # revocation. Native 100 ms watchdog and all limits are unchanged.
        with self._condition:
            while True:
                now = time.monotonic_ns()
                if self._closing or generation != self._generation[side] or now >= deadline:
                    return False
                remaining = self._wire_ack_ns[side]+10_000_000-now
                if remaining <= 0:
                    return True
                self._condition.wait(min(remaining, deadline-now)/1e9)

    def _drop_target(self, side, reason):
        with self._condition:
            self._dropped_targets[side] += 1
        self._log('target_not_sent', side=side, reason=reason, replayed=False)

    def status(self):
        with self._condition:
            return dict(started=self._started, closed=self._closed,
                        errors=dict(self._errors), pending_sides=sorted(self._pending),
                        idle_busy_reads=dict(self._idle_busy),
                        idle_expired_reads=dict(self._idle_expired),
                        query_inflight=sorted(self._reading),
                        dropped_unsent_targets=dict(self._dropped_targets),
                        rate_limited_targets=dict(self._rate_limited_targets),
                        owner_acknowledged_target_rad={s: wire_position(self.profiles[s], payload)
                            if payload is not None else None for s, payload in self._wire_payload.items()},
                        possibly_enabled=dict(self._possibly_enabled),
                        disabled_acknowledged=dict(self._disabled_ack))

    def close(self):
        with self._condition:
            if self._closed:
                return dict(closed=True, errors=[s+": "+v for s, v in self._errors.items()],
                            disabled_acknowledged=dict(self._disabled_ack))
            # Cancel queued targets; workers perform a final disable only if
            # an enable was actually attempted by this session.
            self._closing = True
            self._pending.clear()
            for side in self._generation:
                self._generation[side] += 1
            self._condition.notify_all()
        for side, thread in self._threads.items():
            thread.join(timeout=1.)
            if thread.is_alive():
                self._errors[side] = "owner worker did not stop; hardware state unconfirmed"
        self._closed = all(not thread.is_alive() for thread in self._threads.values())
        result = dict(closed=self._closed, errors=[s+": "+v for s, v in self._errors.items()],
                      disabled_acknowledged=dict(self._disabled_ack),
                      possibly_enabled=dict(self._possibly_enabled))
        self._log("owner_session_closed", **result)
        return result
