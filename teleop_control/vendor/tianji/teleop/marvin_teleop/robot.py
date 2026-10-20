"""Per-arm SDK mode transitions; the control loop never waits for an idle/enable ACK."""

import ctypes
from collections import deque
import time
import numpy as np

# Vendor FxRtCSDef.h ArmState: 101/109 are normal transitional states.
POSITION_TRANSITION = 101
IDLE_TRANSITION = 109
MODE_TIMEOUT_S = 2.0


def controller_state_message(side, state):
    message = f"{side}: controller state={state}"
    if state["err_code"] == 15:
        message += (
            "；厂家错误码 15=PDO数据异常（ARM_ERR_PdoAbnormal）。"
            "请在厂家软件检查该臂总线/伺服诊断，排除故障后清错并重新只读预检；"
            "仅凭此码不能确定具体故障部件"
        )
    return message


class CommandBusy(RuntimeError):
    """Native send slot is occupied; no new joint command has been queued."""


class CommandExpired(RuntimeError):
    """A VR goal expired before send; discard it and send a measured hold."""


class Robot:
    def __init__(self, config):
        from SDK_PYTHON.fx_robot import Marvin_Robot, DCSS

        self.api, self.dcss = Marvin_Robot(), DCSS()
        self.config = config
        self.enabled_sides = set()
        self.pending_sides = {}
        self.stopping_sides = {}
        self.linked = False
        self.frames = {s: None for s in ("left", "right")}
        self.changed = {s: 0.0 for s in self.frames}
        if not self.api.connect(config["robot_ip"]):
            self.api.release_robot()
            raise RuntimeError(
                "robot SDK connect failed: check IP, network and other controller clients"
            )
        self.linked = True
        # OnLinkTo leaves native local logging enabled, including one printf per
        # CAN/485 reply. Disable SDK chatter before the gripper polling starts;
        # application diagnostics and collector output remain on the terminal.
        self.api.local_log_switch("0")
        self.changed = {s: time.monotonic() for s in self.frames}
        self.q = None
        self.actual_states = {}
        self.velocities = {}

    @property
    def enabled(self):
        return bool(self.enabled_sides or self.pending_sides or self.stopping_sides)

    def wait_ready(self):
        deadline, good, last_error = time.monotonic() + 3, 0, ""
        while time.monotonic() < deadline:
            try:
                self.read()
                good += 1
                if good >= 10:
                    self.check_servo_errors()
                    return
            except ValueError as error:
                good, last_error = 0, str(error)
            time.sleep(0.02)
        # Even a controller in ERROR may still return useful servo diagnostics.
        # Query once on startup failure, never in the active control loop.
        try:
            detail = f"；伺服原始错误码={self.read_servo_errors()}"
        except Exception as error:
            detail = f"；伺服诊断读取失败={error}"
        raise RuntimeError("robot startup check failed: " + last_error + detail)

    @staticmethod
    def checked(result, name):
        if not result:
            raise RuntimeError(f"SDK {name} failed")

    def _clear_buffer(self, name):
        # OnSetSend only queues the native buffer (m_SendTag=100). Until its
        # worker transmits it, OnClearSet returns false; this is normal busy,
        # especially when enabling one arm while streaming the other.
        deadline = time.monotonic() + 0.01
        while not self.api.clear_set():
            if time.monotonic() >= deadline:
                raise CommandBusy(f"SDK {name}: send buffer busy for 10 ms")
            time.sleep(0.0005)

    def _observe_mode(self, side, serial, state, now):
        self.actual_states[side] = state
        if side in self.stopping_sides:
            deadline, old_serial = self.stopping_sides[side]
            if serial != 0 and serial != old_serial and state == 0:
                del self.stopping_sides[side]
                self.actual_states[side] = 0
            elif now > deadline:
                raise RuntimeError(
                    f"{side}: fresh idle acknowledgement missing "
                    f"(state={state}, frame={serial}, request_frame={old_serial}); "
                    "use physical emergency stop"
                )
        elif side in self.pending_sides:
            deadline, old_serial = self.pending_sides[side]
            if serial != 0 and serial != old_serial and state == 1:
                del self.pending_sides[side]
                self.enabled_sides.add(side)
            elif now > deadline:
                raise RuntimeError(
                    f"{side}: position mode enable timed out "
                    f"(state={state}, frame={serial}, request_frame={old_serial})"
                )
        elif side in self.enabled_sides and state != 1:
            raise ValueError(f"{side}: position mode lost")

    def read(self, *, allow_faults=False):
        data, now = self.api.subscribe(self.dcss), time.monotonic()
        q = {}
        # Capture both lanes before validating either: a stale left lane must
        # not hide whether the right lane and controller states are advancing.
        self.feedback_status = {}
        for side, channel in self.config["arm_channels"].items():
            i = 0 if channel == "A" else 1
            out, state = data["outputs"][i], data["states"][i]
            serial = out["frame_serial"]
            if serial != 0 and serial != self.frames[side]:
                self.changed[side], self.frames[side] = now, serial
            self.actual_states[side] = state["cur_state"]
            self.feedback_status[side] = {
                "channel": channel,
                "frame": serial,
                "unchanged_s": round(now - self.changed[side], 4),
                "state": dict(state),
            }
        if not hasattr(self, "feedback_history"):
            self.feedback_history = deque(maxlen=200)
        self.feedback_history.append({"monotonic_s": now, "arms": self.feedback_status})
        for side, channel in self.config["arm_channels"].items():
            i = 0 if channel == "A" else 1
            out, state = data["outputs"][i], data["states"][i]
            self.actual_states[side] = state["cur_state"]
            self.velocities[side] = np.asarray(out["fb_joint_vel"], dtype=float)
            if (
                self.velocities[side].shape != (7,)
                or not np.isfinite(self.velocities[side]).all()
            ):
                raise ValueError("invalid joint velocity feedback")
            serial = out["frame_serial"]
            if serial == 0 or now - self.changed[side] > self.config["robot_timeout_s"]:
                raise ValueError(
                    f"{side}: robot UDP feedback not advancing; "
                    f"SDK缓存观测={self.feedback_status}"
                )
            allowed_states = {0, 1}
            if side in self.pending_sides:
                allowed_states.add(POSITION_TRANSITION)
            if side in self.stopping_sides:
                # Release can arrive while the preceding enable is in flight.
                allowed_states.update((POSITION_TRANSITION, IDLE_TRANSITION))
            if not allow_faults and (state["err_code"] != 0 or state["cur_state"] not in allowed_states):
                raise ValueError(controller_state_message(side, state))
            if not allow_faults:
                self._observe_mode(side, serial, state["cur_state"], now)
            q[side] = np.asarray(out["fb_joint_pos"], dtype=float)
            if q[side].shape != (7,) or not np.isfinite(q[side]).all():
                raise ValueError("invalid joint feedback")
        self.q = q
        return q

    def request_recovery_idle(self):
        """Explicit recovery only: clear latched errors once, request both idle.

        OnClearErr_A/B are void C functions; their return value is NOT an ACK.
        The recovery state machine must verify new, error-free idle feedback.
        """
        self._clear_buffer("recovery clear_set")
        for side, channel in self.config['arm_channels'].items():
            getattr(self.api.robot, f'OnClearErr_{channel}').restype = None
            self.api.clear_error(channel)
            self.checked(self.api.set_state(channel, 0), "recovery idle")
        # Mark both arms as owned until a fresh idle ACK, including an arm that
        # entered error before this process could record its enabled state.
        for side in self.config['arm_channels']:
            self.stopping_sides[side] = (time.monotonic() + 8.0, self.frames[side])
        self.checked(self.api.send_cmd(), "recovery idle send")

    def stage_recovery_hold(self, side):
        """Separate measured-hold transaction before the position-mode request."""
        self._clear_buffer("recovery hold")
        channel = self.config['arm_channels'][side]
        self.checked(self.api.set_vel_acc(channel, 100, 100), "recovery velocity settings")
        self.checked(self.api.set_joint_cmd_pose(channel, self.q[side].tolist()), "recovery initial hold")
        self.checked(self.api.send_cmd(), "recovery hold send")

    def check_servo_errors(self):
        for side, errors in self.read_servo_errors().items():
            if any(errors):
                raise ValueError(f"{side}: servo errors {errors}")

    def read_servo_errors(self):
        # Synchronous parameter queries run at startup. During control, errors,
        # mode and freshness are checked from UDP for both arms every cycle.
        result = {}
        for side, channel in self.config["arm_channels"].items():
            errors = (ctypes.c_long * 7)()
            fn = getattr(self.api.robot, f"OnGetServoErr_{channel}")
            fn.argtypes = [ctypes.POINTER(ctypes.c_long * 7)]
            fn(ctypes.byref(errors))
            result[side] = list(errors)
        return result

    def enable(self, sides=None):
        selected = set(self.config["arm_channels"] if sides is None else sides)
        if not selected <= self.config["arm_channels"].keys():
            raise ValueError("unknown arm")
        if selected & self.stopping_sides.keys():
            raise ValueError("wait for idle acknowledgement before enabling again")
        selected -= self.enabled_sides | self.pending_sides.keys()
        q = self.read()
        if not selected:
            return q
        self._clear_buffer("clear_set")
        for side in sorted(selected):
            channel = self.config["arm_channels"][side]
            self.checked(
                self.api.set_joint_cmd_pose(channel, q[side].tolist()),
                "set initial hold",
            )
            # Python already limits every streamed step's speed/acceleration.
            # Do not add a second 10%-speed profile inside the controller.
            self.checked(self.api.set_vel_acc(channel, 100, 100), "set_vel_acc")
            self.checked(self.api.set_state(channel, 1), "set_state position")
        # Mark before send: an ambiguous send failure still requires stop cleanup.
        for side in selected:
            self.pending_sides[side] = (
                time.monotonic() + MODE_TIMEOUT_S,
                self.frames[side],
            )
        self.checked(self.api.send_cmd(), "enable send")
        return q  # read() observes startup ACKs without blocking VR/status.

    def command(self, q, deadline=None):
        if not q:
            return
        if not set(q) <= self.enabled_sides:
            raise ValueError("joint command for an arm not enabled by this runtime")
        self._clear_buffer("clear_set")
        for side, joints in q.items():
            self.checked(
                self.api.set_joint_cmd_pose(
                    self.config["arm_channels"][side], joints.tolist()
                ),
                "set_joint_cmd_pose",
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise CommandExpired("VR command expired while waiting for SDK send buffer")
        self.checked(self.api.send_cmd(), "joint send")

    def stop(self, sides=None):
        # Explicit sides => nonblocking low-level disable. No argument =>
        # global stop/shutdown, wait boundedly for every requested idle ACK.
        wait = sides is None
        owned = (
            self.enabled_sides | self.pending_sides.keys() | self.stopping_sides.keys()
        )
        selected = owned if wait else owned & set(sides)
        newly_stopping = selected - self.stopping_sides.keys()
        if newly_stopping:
            self._clear_buffer("stop clear_set")
            for side in sorted(newly_stopping):
                self.checked(
                    self.api.set_state(self.config["arm_channels"][side], 0),
                    "stop idle",
                )
            for side in newly_stopping:
                self.stopping_sides[side] = (
                    time.monotonic() + MODE_TIMEOUT_S,
                    self.frames[side],
                )
                self.enabled_sides.discard(side)
                self.pending_sides.pop(side, None)
            self.checked(self.api.send_cmd(), "stop send")
        if wait:
            while self.stopping_sides:
                # Shutdown still checks fresh mode ACKs even if an ordinary read
                # would reject a servo error or state before reaching that check.
                data, now = self.api.subscribe(self.dcss), time.monotonic()
                for side in list(self.stopping_sides):
                    i = 0 if self.config["arm_channels"][side] == "A" else 1
                    self._observe_mode(
                        side,
                        data["outputs"][i]["frame_serial"],
                        data["states"][i]["cur_state"],
                        now,
                    )
                if self.stopping_sides:
                    time.sleep(0.005)

    def close(self):
        try:
            self.stop()
        finally:
            if self.linked:
                self.api.release_robot()
                self.linked = False


class SimRobot:
    def __init__(self):
        self.q = {
            s: np.array([0.0, -35.0, 0.0, -65.0, 0.0, 15.0, 0.0])
            for s in ("left", "right")
        }
        self.enabled_sides = set()
        self.pending_sides = {}
        self.stopping_sides = {}

    @property
    def enabled(self):
        return bool(self.enabled_sides)

    def read(self):
        return {s: q.copy() for s, q in self.q.items()}

    def enable(self, sides=None):
        self.enabled_sides.update(self.q if sides is None else sides)
        return self.read()

    def command(self, q, deadline=None):
        if not set(q) <= self.enabled_sides:
            raise ValueError("command to disabled simulated arm")
        self.q.update({s: x.copy() for s, x in q.items()})

    def stop(self, sides=None):
        self.enabled_sides.difference_update(
            self.enabled_sides.copy() if sides is None else sides
        )

    def close(self):
        self.stop()
