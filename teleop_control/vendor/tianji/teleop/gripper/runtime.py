"""Gripper worker at the arm command rate, with independent feedback watchdogs."""

import copy
import math
import threading
import time
from collections import deque
from .damiao import Driver, SocketCAN, Simulation, TransportBusy
from .config import bounded_position, travel_bounds
from .sampling import observed_hz
from .motion import ramp


class Control:
    def __init__(self, motors, driver, timeout):
        self.motors, self.driver, self.timeout = motors, driver, timeout
        self.states = {
            s: {
                "state": "WAIT_RELEASE",
                "target_rad": None,
                "command_velocity_rad_s": 0.0,
                "reason": "先松开扳机，再按下接管",
            }
            for s in motors
        }
        self.context = None
        self.fault = ""
        self.last_tick = None
        self.enabling_since = {}
        self.period = None

    def disable(self, reason):
        # Attempt every motor even if a CAN write fails for one side.
        errors = []
        for side, state in self.states.items():
            state.update(state="WAIT_RELEASE", reason=reason, target_rad=None, command_velocity_rad_s=0.0)
            try:
                self.driver.disable(side)
            except OSError as error:
                errors.append(error)
        self.enabling_since.clear()
        if errors:
            if all(isinstance(error, TransportBusy) for error in errors):
                raise errors[0]
            raise RuntimeError("; ".join(map(str, errors)))

    def step(self, sample, now):
        dt = (
            min(0.04, max(0, now - self.last_tick)) if self.last_tick is not None else 0
        )
        self.last_tick = now
        # Drain feedback even while waiting for VR, avoiding stale queued replies.
        self.driver.poll()
        feedback_now = max(now, time.monotonic())
        if sample and sample["emergency"] and not self.fault:
            self.fault = "停止请求或机械臂故障；重启前排除故障"
        if self.fault:
            self.disable(self.fault)
            return
        if (
            not sample
            or not sample["valid"]
            or not 0 <= now - sample["submitted"] <= self.timeout
            or not 0 <= now - sample["received"] <= self.timeout
        ):
            self.disable("VR/主循环数据过期；恢复后先松开扳机")
            return
        context = sample["context"]
        if context != self.context:
            self.disable("新的 VR 会话；先松开扳机")
            self.context = context
        busy = None
        for side, m in self.motors.items():
            if self.period is not None:
                # Share the cycle fairly between endpoints, including duplicate
                # separators. A busy left endpoint must still let right send.
                deadline = min(now + self.period,
                               time.monotonic() + self.period / len(self.motors))
                self.driver.set_send_deadline(side, deadline)
            try:
                self._motor_step(side, m, sample, now, feedback_now, dt)
            except TransportBusy as error:
                busy = error
            finally:
                if self.period is not None:
                    self.driver.set_send_deadline(side, None)
        if busy is not None:
            raise busy

    def _motor_step(self, side, m, sample, now, feedback_now, dt):
        s = self.states[side]
        f = self.driver.feedback.get(side)
        if f is None or not 0 <= feedback_now - f.received <= m["feedback_timeout_s"]:
            age = "未收到反馈" if f is None else f"反馈年龄={(feedback_now - f.received) * 1000:.1f} ms"
            raise RuntimeError(
                f"{side}: motor feedback timeout（{age}，"
                f"上限={m['feedback_timeout_s'] * 1000:.0f} ms，阶段={s['state']}）"
            )
        if f.state not in (0, 1):
            raise RuntimeError(f"{side}: motor fault 0x{f.state:x}")
        if max(f.mos_c, f.rotor_c) >= m["max_temperature_c"]:
            raise RuntimeError(f"{side}: motor temperature limit")
        lo, hi = sorted((m["open_rad"], m["closed_rad"]))
        if not lo - 0.001 <= f.position <= hi + 0.001:
            raise RuntimeError(f"{side}: feedback outside calibrated travel")
        t = sample["triggers"].get(side)
        if type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= 1:
            self.driver.disable(side)
            s.update(
                state="WAIT_RELEASE",
                reason="本侧扳机数据缺失/无效",
                target_rad=None,
            )
            return
        if s["state"] == "WAIT_RELEASE":
            self.driver.disable(side)
            if t <= 0.05:
                s.update(state="READY", reason="等待按下扳机；当前未使能")
        elif s["state"] == "READY":
            if f.state != 0:
                self.driver.disable(side)
                s["reason"] = "等待电机失能反馈，尚未接管"
            elif t > 0.05:
                s["target_rad"] = bounded_position(m, f.position)
                s["command_velocity_rad_s"] = 0.0
                self.driver.enable(side, s["target_rad"])
                self.enabling_since[side] = now
                s.update(state="ENABLING", reason="等待电机使能反馈")
            else:
                self.driver.disable(side)
        elif s["state"] == "ENABLING":
            if f.state == 1 and f.received >= self.enabling_since[side]:
                s.update(
                    state="ACTIVE", reason="扳机 0=打开，1=闭合；握把可独立松开"
                )
            elif now - self.enabling_since[side] > m["feedback_timeout_s"]:
                raise RuntimeError(f"{side}: enable acknowledgement timeout")
            self.driver.send_position(side, s["target_rad"])
        else:
            if f.state != 1:
                raise RuntimeError(f"{side}: motor unexpectedly disabled")
            lo, hi = travel_bounds(m)
            opened, closed = (lo, hi) if m["open_rad"] < m["closed_rad"] else (hi, lo)
            goal = opened + t * (closed - opened)
            # A trigger step must not instantly jump to maximum motor speed.
            # Commit trajectory state only after the transport accepted it.
            target, velocity = ramp(s["target_rad"], s["command_velocity_rad_s"], goal,
                                    min(dt, .02), m["speed_rad_s"],
                                    m.get("accel_rad_s2", 20.0), lo, hi)
            self.driver.send_position(side, target)
            s["target_rad"] = target
            s["command_velocity_rad_s"] = velocity

    def status(self):
        return {
            "fault": self.fault,
            "motors": {
                side: dict(
                    s,
                    kp=self.motors[side].get("kp"),
                    kd=self.motors[side].get("kd"),
                    control_mode=self.motors[side].get("control_mode", 4),
                    command_range_rad=travel_bounds(self.motors[side]),
                    display_endpoints_rad={
                        key: bounded_position(self.motors[side], self.motors[side][key])
                        for key in ("open_rad", "closed_rad")
                    },
                    speed_rad_s=self.motors[side]["speed_rad_s"],
                    accel_rad_s2=self.motors[side].get("accel_rad_s2", 20.0),
                    calibration_endpoints_rad={key:self.motors[side][key]
                                               for key in ("open_rad", "closed_rad")},
                    current_fraction=self.motors[side]["current_fraction"],
                    max_current_a=self.motors[side].get("max_current_a"),
                    current_limit=getattr(self.driver, "current_limit_reports", {}).get(side),
                    feedback=self.driver.feedback[side].status()
                    if side in self.driver.feedback
                    else None,
                )
                for side, s in self.states.items()
            },
        }


def refresh_disabled_feedback(driver, motors):
    """Finish setup with fresh disabled acknowledgements from every selected motor.

    Per-motor preflight runs sequentially. Its first motor's feedback may already
    be stale by the time the last motor finishes reading registers. Do not feed
    those old samples into the live watchdog or enable a motor to refresh them.
    """
    requested = {}
    for side, motor in motors.items():
        requested[side] = time.monotonic()
        driver.set_send_deadline(side, requested[side] + motor["feedback_timeout_s"])
        try:
            driver.disable(side)
        finally:
            driver.set_send_deadline(side, None)
    deadline = min(requested[s] + m["feedback_timeout_s"] for s, m in motors.items())
    while True:
        driver.poll()
        now = time.monotonic()
        pending = []
        for side, motor in motors.items():
            feedback = driver.feedback.get(side)
            if (feedback is None or feedback.state != 0
                    or feedback.received < requested[side]
                    or not 0 <= now - feedback.received <= motor["feedback_timeout_s"]):
                pending.append(side)
        if now >= deadline:
            raise RuntimeError("gripper: startup disabled feedback timeout: " + ", ".join(pending or motors))
        if not pending:
            return
        time.sleep(min(0.001, deadline - now))


class Grippers:
    def __init__(self, config, live, timeout, robot=None, *, rate_hz=100):
        if type(rate_hz) not in (int, float) or not math.isfinite(rate_hz) or not 10 <= rate_hz <= 100:
            raise ValueError("gripper: rate_hz must be in [10, 100]")
        self.rate_hz = rate_hz
        self.period = 1 / rate_hz
        self.live = live
        if live and config["transport"] == "marvin_can":
            from .marvin_can import RoutedDriver

            self.driver = RoutedDriver(robot, config)
        else:
            self.driver = (
                Driver(SocketCAN(config["channel"]), config["motors"])
                if live
                else Simulation(config["motors"])
            )
        try:
            self.driver.preflight()
            refresh_disabled_feedback(self.driver, config["motors"])
        except BaseException:
            self.driver.close()
            raise
        self.control = Control(config["motors"], self.driver, timeout)
        self.control.period = self.period
        self.busy_since = None
        self.busy_timeout = min(m["feedback_timeout_s"] for m in config["motors"].values())
        self.deferred_sends = 0
        self.lock = threading.Lock()
        self.sample = None
        self.value = self.control.status()
        self.pending_feedback = {side: deque(maxlen=512) for side in config["motors"]}
        self.feedback_queue_dropped = dict.fromkeys(config["motors"], 0)
        self.feedback_timing = self.driver.feedback_timing
        self.cycle_stamps = deque(maxlen=101)
        self.cycles = 0
        self.missed_deadlines = 0
        self.max_cycle_s = 0.0
        # Preflight replies precede runtime/recording; do not label those as
        # new samples from the first control cycle.
        self.driver.drain_feedback()
        self.shutdown = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="damiao-grippers", daemon=True
        )
        self.thread.start()

    def submit(self, frame, connected, emergency):
        sample = {
            "submitted": time.monotonic(),
            "received": frame.received if frame else 0,
            "context": (frame.session, frame.safety_epoch) if frame else None,
            "valid": bool(connected and frame and frame.focused and frame.tracked),
            "emergency": bool(emergency or (frame and frame.stop)),
            "triggers": dict(frame.triggers) if frame else {},
        }
        with self.lock:
            # Preserve a stop even when the publisher overwrites samples before the worker runs.
            sample["emergency"] |= bool(self.sample and self.sample["emergency"])
            self.sample = sample

    def _step(self, sample, started):
        try:
            self.control.step(sample, started)
        except TransportBusy:
            self.deferred_sends += 1
            if self.busy_since is None:
                self.busy_since = started
            if started - self.busy_since >= self.busy_timeout:
                raise RuntimeError("gripper: endpoint persistently busy; hardware watchdog required")
            return
        self.busy_since = None

    def run(self):
        next_deadline = time.monotonic()
        while not self.shutdown.is_set():
            started = time.monotonic()
            with self.lock:
                sample = self.sample
            try:
                self._step(sample, started)
            except Exception as error:
                self.control.fault = str(error)
                try:
                    self.control.disable(str(error))
                except Exception as stop_error:
                    self.control.fault += "; disable failed: " + str(stop_error)
            with self.lock:
                self.value = self.control.status()
                for side, rows in self.driver.drain_feedback().items():
                    pending = self.pending_feedback[side]
                    self.feedback_queue_dropped[side] += max(0, len(pending) + len(rows) - pending.maxlen)
                    pending.extend(rows)
                self.feedback_timing = self.driver.feedback_timing
                self.cycle_stamps.append(started)
                self.cycles += 1
                finished = time.monotonic()
                self.max_cycle_s = max(self.max_cycle_s, finished - started)
                next_deadline += self.period
                if finished > next_deadline:
                    missed = int((finished - next_deadline) / self.period) + 1
                    self.missed_deadlines += missed
                    next_deadline += missed * self.period
            # Fixed deadlines avoid accumulating sleep/scheduling drift. A
            # slow endpoint skips missed slots instead of bursting stale targets.
            self.shutdown.wait(max(0, next_deadline - time.monotonic()))

    def drain_feedback(self):
        """Return each new received frame once; never query the motor here."""
        with self.lock:
            result = {side: list(rows) for side, rows in self.pending_feedback.items()}
            for rows in self.pending_feedback.values():
                rows.clear()
            return result

    def status(self):
        with self.lock:
            return dict(
                copy.deepcopy(self.value), mode="live" if self.live else "simulation",
                deferred_sends=self.deferred_sends,
                control_timing={"requested_hz": self.rate_hz,
                                "actual_hz": observed_hz(self.cycle_stamps),
                                "cycles": self.cycles,
                                "missed_deadlines": self.missed_deadlines,
                                "max_cycle_ms": 1000 * self.max_cycle_s},
                feedback_timing=copy.deepcopy(self.feedback_timing),
                feedback_queue_dropped={
                    side: dropped + self.feedback_timing[side]["queue_dropped"]
                    for side, dropped in self.feedback_queue_dropped.items()},
            )

    def close(self):
        self.shutdown.set()
        self.thread.join(timeout=1)
        self.driver.close()
