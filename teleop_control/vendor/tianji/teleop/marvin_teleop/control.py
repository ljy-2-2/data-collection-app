"""Independent arm clutches and heading anchors, with shared collision checks."""

from dataclasses import dataclass, field
import time
import numpy as np
from .geometry import angle, relative_target
from .mapping import same_facing_map
from .protocol import GRIP_ON, GRIP_OFF
from .robot import CommandExpired, CommandBusy

SIDES = ("left", "right")
NAMES = {"left": "左臂", "right": "右臂"}


def check_start_pose(models, joints, margin, sides=None, allow_singular=False):
    selected = models.arms if sides is None else sides
    issues = []
    for side in selected:
        arm, name = models.arms[side], NAMES[side]
        if not arm.valid_joints(joints[side]):
            issues.append(f"{name}当前关节角接近或超出限位")
        elif allow_singular:
            arm.fk(joints[side])
        elif abs(joints[side][3] - arm.elbow_singularity_deg) < 0.1:
            issues.append(
                f"{name}肘关节 J4={joints[side][3]:.4f}°，接近厂家伸展分界 {arm.elbow_singularity_deg:.4f}°"
            )
        else:
            try:
                arm.ik(arm.fk(joints[side]), joints[side])
            except ValueError as error:
                issues.append(
                    f"{name}：{error}，厂家逆解标志={getattr(arm, 'last_vendor_flags', {})}"
                )
    if issues:
        raise ValueError(
            "；".join(issues)
            + "。可运行 ./start-live.sh --prepare，按对应握把准备该臂；也可用厂家软件调整后再检查"
        )
    reason = models.collision_reason(joints, margin)
    if reason:
        raise ValueError(reason)


@dataclass
class ArmControl:
    mode: str = "WAIT_RELEASE"
    reason: str = "先松开本侧握把，再按住控制"
    released: bool = False
    release_epoch: object = None
    preparing: bool = False
    plan: object = None
    elapsed: float = 0.0
    commanded: object = None
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(7))
    next_velocity: np.ndarray = field(default_factory=lambda: np.zeros(7))
    anchor: object = None
    basis: object = None
    calibration_head: object = None
    joint_anchor: object = None
    previous_hand: object = None
    sequence: object = None
    pause_reason: str = ""
    pause_count: int = 0
    target_pose: object = None
    constrained_count: int = 0
    position_error_m: object = None
    rotation_error_rad: object = None
    ik_detail: str = ""
    elbow_distance_deg: object = None
    jacobian_sigma_min: object = None
    jacobian_condition: object = None
    rotation_weight_m: object = None
    shoulder_motion_weight: object = None
    elbow_branch_sign: object = None
    elbow_assist: bool = False
    limit_assist: bool = False
    joint_limit_margin_deg: object = None
    speed_limited: bool = False
    target_clamped: bool = False


class Controller:
    def __init__(self, models, robot, config, prepare=False):
        self.models, self.robot, self.cfg = models, robot, config
        self.controlled_sides = tuple(config.get("controlled_sides", SIDES))
        if not self.controlled_sides or not set(self.controlled_sides) <= set(SIDES):
            raise ValueError("unknown controlled arm")
        self.arms = {s: ArmControl(preparing=prepare) for s in self.controlled_sides}
        for arm in models.arms.values():
            if config["max_joint_speed_deg_s"] > min(arm.velocity_limits) or config[
                "max_joint_accel_deg_s2"
            ] > min(arm.acceleration_limits):
                raise ValueError("motion settings exceed vendor PNVA limits")
        self.state, self.reason = "WAIT_RELEASE", "左右握把分别控制对应机械臂"
        self.session = self.safety_epoch = self.last_tick = None
        self.fault = False
        self.send_busy_since = None
        self.deferred_sends = 0
        self.slow_cycles = 0
        self.power_started = False
        self.holds = {}
        self.measured = None
        self.suspend_reason = None
        self.resume_sides = set()
        self.resume_since = None

    def start_session(self):
        """Enable once at measured pose; clutches never change servo power."""
        if self.power_started:
            return
        q = self.robot.read()
        check_start_pose(
            self.models, q, self.cfg["collision_margin_m"], allow_singular=True
        )
        if any(
            np.max(np.abs(v)) > 0.5
            for v in getattr(self.robot, "velocities", {}).values()
        ):
            raise RuntimeError("启动时机械臂仍在运动，不能建立静止保持目标")
        self.holds = {s: q[s].copy() for s in SIDES}
        self.measured = q
        hold = self.robot.enable(self.controlled_sides)
        self.holds = {s: hold[s].copy() for s in SIDES}
        self.power_started = True

    def _input_lost(self, now):
        if self.resume_since is None:
            self.resume_since = now
            self.resume_sides = self.active_sides.copy()
        self.pause("VR 数据过期：保持当前位置，短暂断流恢复后重新锚定")
        if now - self.resume_since > self.cfg.get("resume_gap_s", 0.6):
            self.resume_sides.clear()

    @property
    def preparing(self):
        return any(a.preparing for a in self.arms.values())

    @property
    def released(self):
        return any(a.released for a in self.arms.values())

    @property
    def active_sides(self):
        return {s for s, a in self.arms.items() if a.mode == "ACTIVE"}

    def status(self):
        return {
            s: {
                "state": a.mode,
                "reason": a.reason,
                "preparing": a.preparing,
                "pause_reason": a.pause_reason,
                "pause_count": a.pause_count,
                "constraint_count": a.constrained_count,
                "position_error_m": a.position_error_m,
                "rotation_error_rad": a.rotation_error_rad,
                "ik_detail": a.ik_detail,
                "elbow_distance_deg": a.elbow_distance_deg,
                "jacobian_sigma_min": a.jacobian_sigma_min,
                "jacobian_condition": a.jacobian_condition,
                "rotation_weight_m": a.rotation_weight_m,
                "shoulder_motion_weight": a.shoulder_motion_weight,
                "elbow_branch_sign": a.elbow_branch_sign,
                "elbow_assist": a.elbow_assist,
                "limit_assist": a.limit_assist,
                "joint_limit_margin_deg": a.joint_limit_margin_deg,
                "speed_limited": a.speed_limited,
                "target_clamped": a.target_clamped,
                "command_velocity_deg_s": a.velocity.tolist(),
                "joint_anchor_deg": a.joint_anchor.tolist()
                if a.joint_anchor is not None
                else None,
                "commanded_joints_deg": a.commanded.tolist()
                if a.commanded is not None
                else None,
                "target_pose": a.target_pose.tolist()
                if a.target_pose is not None
                else None,
                "hand_anchor": a.anchor[0].tolist() if a.anchor else None,
                "tcp_anchor": a.anchor[1].tolist() if a.anchor else None,
                "mapping_basis": a.basis.tolist() if a.basis is not None else None,
                "translation_z_sign": self.cfg.get('teleop_translation_z_sign', {}).get(s, 1),
                "translation_xy_sign": self.cfg.get('teleop_translation_xy_sign', {}).get(s, 1),
                "calibration_head": a.calibration_head.tolist()
                if a.calibration_head is not None
                else None,
                "flange_anchor": self.models.arms[s].flange_fk(a.joint_anchor).tolist()
                if a.joint_anchor is not None
                else None,
                "control_point": self.models.arms[s].control_point_name,
                "progress": min(1.0, a.elapsed / a.plan.duration) if a.plan else None,
            }
            for s, a in self.arms.items()
        }

    def _refresh(self):
        modes = {a.mode for a in self.arms.values()}
        self.state = (
            "FAULT"
            if self.fault
            else next(
                (m for m in ("ACTIVE", "ARMING", "READY") if m in modes), "WAIT_RELEASE"
            )
        )
        if not self.fault:
            self.reason = "；".join(
                f"{NAMES[s]}：{a.reason}" for s, a in self.arms.items()
            )

    @staticmethod
    def _reset(a, reason):
        a.mode, a.reason, a.released = "WAIT_RELEASE", reason, False
        a.plan = a.anchor = a.commanded = a.previous_hand = a.sequence = None
        a.basis = a.calibration_head = None
        a.joint_anchor = None
        a.target_pose = None
        a.position_error_m = a.rotation_error_rad = None
        a.ik_detail, a.elbow_distance_deg = "", None
        a.shoulder_motion_weight = None
        a.jacobian_sigma_min = a.jacobian_condition = a.rotation_weight_m = None
        a.elbow_branch_sign, a.elbow_assist = None, False
        a.limit_assist, a.joint_limit_margin_deg = False, None
        a.speed_limited = a.target_clamped = False
        a.elapsed = 0.0
        a.velocity = np.zeros(7)
        a.next_velocity = np.zeros(7)

    def pause_side(self, side, reason):
        self.holds[side] = self.measured[side].copy()
        self._reset(self.arms[side], reason)
        self.arms[side].pause_reason = reason
        self.arms[side].pause_count += 1

    def pause(self, reason, fault=False):
        if fault:
            self.robot.stop()
        elif self.suspend_reason != reason or self.active_sides:
            if self.measured is not None:
                self.holds = {s: self.measured[s].copy() for s in SIDES}
        self.suspend_reason = reason
        for a in self.arms.values():
            self._reset(a, reason)
        self.fault |= fault
        self.reason = reason
        self._refresh()

    def _start(self, side, frame, q):
        a = self.arms[side]
        if side in self.robot.stopping_sides:
            a.reason = "正在确认本侧停止，确认后允许接管"
            return
        if (
            np.max(np.abs(getattr(self.robot, "velocities", {}).get(side, np.zeros(7))))
            > 0.5
        ):
            a.reason = "等待本侧保持稳定，握把保持按住即可接管"
            return
        try:
            if a.preparing:
                if a.plan is None:
                    from .preparation import PreparationPlan

                    a.plan = PreparationPlan(
                        self.models, q, self.cfg, validate=False, sides={side}
                    )
                    a.reason = "正在预检本侧准备路径，保持本侧握把"
                    return
                if not a.plan.validated:
                    # Leave time in the 10 ms cycle for VR reception and the
                    # other arm. Four geometry samples can saturate the loop.
                    a.plan.validate_chunk(self.models, self.cfg, count=1)
                    return
                if np.max(np.abs(q[side] - a.plan.start[side])) > 0.25:
                    a.plan = None
                    return
            else:
                check_start_pose(
                    self.models,
                    q,
                    self.cfg["collision_margin_m"],
                    {side},
                    allow_singular=True,
                )
        except ValueError as error:
            self.pause_side(side, str(error))
            return
        self._activate(side, frame, q)

    def _activate(self, side, frame, q):
        a = self.arms[side]
        if a.preparing and np.max(np.abs(q[side] - a.plan.start[side])) > 0.25:
            self.pause_side(side, "使能前后姿态变化，请松开重新准备")
            return
        if not a.preparing:
            try:
                a.basis = same_facing_map(frame.head, self.models.robot_from_operator)
            except ValueError as error:
                self.pause_side(side, str(error))
                return
            a.calibration_head = frame.head.copy()
        a.commanded = q[side].copy()
        a.joint_anchor = q[side].copy()
        self.holds[side] = q[side].copy()
        a.velocity, a.elapsed = np.zeros(7), 0.0
        a.anchor = (frame.hands[side].copy(), self.models.arms[side].fk(q[side]))
        a.previous_hand, a.sequence = frame.hands[side].copy(), frame.sequence
        a.mode = "ACTIVE"
        a.released = False
        self.suspend_reason = None
        a.reason = (
            "本侧准备中" if a.preparing else "跟随本侧手柄，松开本侧保持（不下电）"
        )

    def _limited(self, a, desired, dt, speed, accel):
        wanted = np.clip((desired - a.commanded) / max(dt, 0.001), -speed, speed)
        a.next_velocity = np.clip(wanted, a.velocity - accel * dt, a.velocity + accel * dt)
        return a.commanded + a.next_velocity * dt

    def _target(self, side, frame, q, dt):
        a, model = self.arms[side], self.models.arms[side]
        if a.preparing:
            a.elapsed += dt
            if a.elapsed >= a.plan.duration:
                settled = np.max(np.abs(q[side] - a.plan.goal[side])) < 0.15
                stationary = (
                    np.max(
                        np.abs(
                            getattr(self.robot, "velocities", {}).get(side, np.zeros(7))
                        )
                    )
                    < 0.5
                )
                if settled and stationary:
                    check_start_pose(
                        self.models, q, self.cfg["collision_margin_m"], {side}
                    )
                    a.preparing = False
                    self.pause_side(side, "准备完成：松开本侧握把，再握住进入遥操")
                    return None
                if a.elapsed > a.plan.duration + 3:
                    raise ValueError("本侧未按时到达准备姿态")
            desired = a.plan.at(a.elapsed)[side]
            a.reason = f"本侧准备中 {min(100, a.elapsed / a.plan.duration * 100):.0f}%（松开本侧停止）"
            return self._limited(a, desired, dt, a.plan.speed, a.plan.accel)
        hand = frame.hands[side]
        if (
            np.linalg.norm(hand[:3, 3] - a.previous_hand[:3, 3])
            > self.cfg["max_hand_step_m"]
            or angle(hand[:3, :3] @ a.previous_hand[:3, :3].T)
            > self.cfg["max_hand_step_rad"]
        ):
            raise ValueError("本侧 VR 位姿跳变，请松开重新接管")
        # Track observed input independently of successful SDK writes. A busy
        # send slot must not accumulate normal hand motion into a false jump.
        a.previous_hand, a.sequence = hand.copy(), frame.sequence
        hand0, flange0 = a.anchor
        target = relative_target(hand, hand0, flange0, a.basis, self.cfg["scale"])
        if self.cfg.get('teleop_translation_xy_sign', {}).get(side, 1) == -1:
            target[:2, 3] = flange0[:2, 3] - (target[:2, 3] - flange0[:2, 3])
        if self.cfg.get('teleop_translation_z_sign', {}).get(side, 1) == -1:
            target[2, 3] = flange0[2, 3] - (target[2, 3] - flange0[2, 3])
        a.target_clamped = False
        a.target_pose = target.copy()
        desired = model.differential_ik(
            target,
            a.commanded,
            dt,
            self.cfg["max_joint_speed_deg_s"],
            velocity=a.velocity,
            max_accel=self.cfg["max_joint_accel_deg_s2"],
            elbow_reference=a.joint_anchor,
            elbow_branch_margin_deg=self.cfg["elbow_branch_margin_deg"],
            rotation_weight_m=self.cfg["ik_rotation_weight_m"],
            shoulder_motion_weight=self.cfg.get("ik_shoulder_motion_weight", 1.0),
            singular_rotation_weight_m=self.cfg["ik_singular_rotation_weight_m"],
            singular_sigma_low=self.cfg["ik_singular_sigma_low"],
            singular_sigma_high=self.cfg["ik_singular_sigma_high"],
            elbow_posture_offset_deg=self.cfg["elbow_posture_offset_deg"],
            elbow_posture_gain_s=self.cfg["elbow_posture_gain_s"],
            joint_limit_soft_margin_deg=self.cfg["joint_limit_soft_margin_deg"],
            joint_limit_reserve_deg=self.cfg["joint_limit_reserve_deg"],
            joint_limit_escape_speed_deg_s=self.cfg["joint_limit_escape_speed_deg_s"],
        )
        actual_flange = model.fk(q[side])
        a.ik_detail = "厂家雅可比约束微分逆解（裸法兰 TCP）"
        a.elbow_distance_deg = abs(float(q[side][3]) - model.elbow_singularity_deg)
        a.jacobian_sigma_min = getattr(model, "last_jacobian_sigma_min", None)
        a.jacobian_condition = getattr(model, "last_jacobian_condition", None)
        a.rotation_weight_m = getattr(model, "last_rotation_weight_m", None)
        a.shoulder_motion_weight = getattr(model, "last_shoulder_motion_weight", None)
        a.elbow_branch_sign = getattr(model, "last_elbow_branch_sign", None)
        a.elbow_assist = getattr(model, "last_elbow_assist", False)
        a.limit_assist = getattr(model, "last_limit_assist", False)
        a.joint_limit_margin_deg = getattr(model, "last_joint_limit_margin_deg", None)
        position_error = np.linalg.norm(target[:3, 3] - actual_flange[:3, 3])
        a.position_error_m = float(position_error)
        a.rotation_error_rad = angle(target[:3, :3] @ actual_flange[:3, :3].T)
        a.reason = (
            "本侧方向受伸直姿态或限位约束，可换方向小幅移动或松开重新定位"
            if position_error > 0.005 and np.max(np.abs(desired - a.commanded)) < 1e-5
            else "跟随本侧手柄，松开本侧保持（不下电）"
        )
        a.next_velocity = (desired - a.commanded) / dt
        a.speed_limited = bool(
            np.max(np.abs(a.next_velocity)) >= 0.98 * self.cfg["max_joint_speed_deg_s"]
        )
        if a.speed_limited and position_error > 0.01:
            a.reason = f"已到关节限速 {self.cfg['max_joint_speed_deg_s']:g}°/s；TCP 位置误差 {position_error * 1000:.1f} mm"
        ik_constrained = getattr(model, "last_ik_constrained", False)
        motion_constrained = getattr(model, "last_motion_constrained", False)
        if ik_constrained or motion_constrained:
            a.constrained_count += 1
        if ik_constrained:
            a.ik_detail = "目标受关节、腕部限位或位姿方向约束，跟随当前可达分量"
            a.reason = a.ik_detail
        if a.elbow_assist and not motion_constrained:
            a.ik_detail = "接近奇异区：优先末端位置并保持启动时的肘部构型"
        if a.limit_assist and not motion_constrained:
            a.ik_detail = "接近关节边界：优先末端位置并主动恢复关节余量"
        if motion_constrained:
            a.reason = "接近关节或腕部限位的刹停边界，减速保持；反向移动手柄可继续"
        return desired

    def tick(self, frame, connected, emergency, now=None):
        started = time.monotonic()
        now = started if now is None else now
        dt = 1 / self.cfg["rate_hz"] if self.last_tick is None else now - self.last_tick
        self.last_tick = now
        if emergency:
            self.pause("停止请求：双臂停止，重启后恢复", True)
            return
        if self.fault:
            return
        if not self.power_started:
            self.start_session()
        self.measured = self.robot.read()
        if self.robot.pending_sides:
            self.state, self.reason = "ARMING", "启动使能确认中，保持当前姿态"
            return
        if set(self.robot.enabled_sides) != set(self.controlled_sides):
            raise RuntimeError("运行期间伺服状态丢失，不自动重新上电")
        q = self.measured
        for side in set(SIDES) - set(self.controlled_sides):
            self.holds[side] = q[side].copy()
        # Scheduling jitter is not a servo fault. Do not integrate a long
        # wall-clock gap into a large joint step; input/robot freshness still
        # independently determine whether a goal may be used.
        if dt > 2 / self.cfg["rate_hz"] + 1e-9:
            self.slow_cycles += 1
        dt = min(2 / self.cfg["rate_hz"], max(dt, 1e-6))
        if (
            not connected
            or frame is None
            or now - frame.received > self.cfg["vr_timeout_s"]
        ):
            self._input_lost(now)
            self._send({}, started, now)
            return
        if frame.session != self.session:
            self.resume_sides.clear()
            self.resume_since = None
            self.pause("新 VR 会话：松开要操作一侧的握把")
            self.session = frame.session
        if not frame.tracked or not frame.focused:
            self.resume_sides.clear()
            self.resume_since = None
            self.pause("head/controller tracking invalid or app not focused")
            self._send({}, started, now)
            return
        if self.safety_epoch is not None and frame.safety_epoch != self.safety_epoch:
            self.resume_sides.clear()
            self.pause("中间发生追踪/焦点丢失，请重新接管")
        self.safety_epoch = frame.safety_epoch
        grips = frame.control_grips if frame.control_grips is not None else frame.grips
        if self.resume_since is not None:
            if now - self.resume_since <= self.cfg.get("resume_gap_s", 0.6):
                for side in self.resume_sides:
                    if grips[side] > GRIP_ON:
                        self.arms[side].released = True
                        self.arms[side].mode = "READY"
            self.resume_sides.clear()
            self.resume_since = None
        # Handle every side's release before any new clutches/targets this cycle.
        for side, a in self.arms.items():
            epoch = frame.release_epochs[side]
            if (
                a.release_epoch is not None
                and epoch != a.release_epoch
                and a.mode in ("ACTIVE", "ARMING")
            ):
                self.pause_side(side, "检测到本侧中间松握把，请重新接管")
            a.release_epoch = epoch
            if grips[side] <= GRIP_OFF:
                if a.mode in ("ACTIVE", "ARMING"):
                    self.pause_side(side, "本侧已暂停，重新握住时以当前位置为起点")
                a.released, a.mode = True, "READY"
        targets = {}
        for side, a in self.arms.items():
            if a.mode == "ARMING":
                if side in self.robot.enabled_sides:
                    self._activate(side, frame, q)
                continue
            if a.mode != "ACTIVE":
                if a.released and grips[side] > GRIP_ON:
                    self._start(side, frame, q)
                continue
            try:
                target = self._target(side, frame, q, max(0.0, dt))
                if target is not None:
                    if not self.models.arms[side].valid_joints(target):
                        raise ValueError("本侧插值指令越过限位")
                    targets[side] = target
            except ValueError as error:
                # IK/workspace/input failures pause this arm; the peer continues.
                self.pause_side(side, str(error))
        self._send(targets, started, now, frame)
        self._refresh()

    def _send(self, targets, started, now, frame=None):
        q = self.measured
        if self.robot.enabled:
            # Both proposed arm targets must be checked together. Inactive arms
            # keep the pose latched from feedback at release.
            proposal = {s: targets.get(s, self.holds[s]) for s in SIDES}
            middle = {s: (proposal[s] + q[s]) / 2 for s in SIDES}
            for candidate in (q, proposal, middle):
                reason = self.models.collision_reason(
                    candidate, self.cfg["collision_margin_m"]
                )
                if reason:
                    raise RuntimeError(reason)  # shared collision risk => both stop
            elapsed = time.monotonic() - started
            if elapsed > 0.05:
                self.slow_cycles += 1
            try:
                if (
                    targets
                    and frame is not None
                    and now + elapsed - frame.received >= self.cfg["vr_timeout_s"]
                ):
                    raise CommandExpired("VR frame expired during computation")
                if not self._dispatch(
                    proposal, now,
                    deadline=started + self.cfg["vr_timeout_s"] - (now - frame.received)
                    if targets and frame is not None
                    else None,
                ):
                    return
            except CommandExpired:
                # Expiry at the compute/send boundary is ordinary input loss,
                # not a servo fault. The expired motion goal is never retried.
                self._input_lost(now)
                proposal, targets = self.holds, {}
                if not self._dispatch(proposal, now):
                    return
            self.holds = {s: value.copy() for s, value in proposal.items()}
            for side, target in targets.items():
                a = self.arms[side]
                a.commanded = target
                a.velocity = a.next_velocity.copy()
                a.previous_hand, a.sequence = frame.hands[side].copy(), frame.sequence

    def _dispatch(self, proposal, now, deadline=None):
        try:
            self.robot.command({s: proposal[s] for s in self.controlled_sides}, deadline=deadline)
        except CommandBusy:
            self.deferred_sends += 1
            if self.send_busy_since is None:
                self.send_busy_since = now
            if now - self.send_busy_since >= self.cfg["robot_timeout_s"]:
                raise RuntimeError("SDK send buffer persistently busy; no accepted control updates")
            # Keep the last accepted pose and integrator velocity. The next
            # cycle recomputes from the latest VR frame, never queues old goals.
            return False
        self.send_busy_since = None
        return True
