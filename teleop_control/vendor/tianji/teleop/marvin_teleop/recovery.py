"""Explicit fault recovery and sequential low-speed seven-joint zeroing.

Mirrors MarvinPlatform's clear-error/idle/measured-hold sequence. Motion stays
on our cancellable control loop, with continuous feedback and collision checks.
No position limit, physical emergency stop or servo fault is bypassed.
"""
import numpy as np

from .point_to_point import pose_plan, pose_at, validate_pose
from .robot import CommandBusy
from .recovery_clearance import RecoveryClearance


class ZeroRecovery:
    def __init__(self, robot, models, config, selected, now):
        self.robot, self.models, self.config = robot, models, config
        self.sides = tuple(sorted(selected, key=lambda s: config['arm_channels'][s]))
        if not self.sides or not set(self.sides) <= set(models.arms):
            raise ValueError('无效的回零机械臂')
        self.phase, self.detail = 'feedback', '回零：读取故障状态与实时关节反馈'
        self.deadline = now + 8.0
        self.last_tick = now
        self.busy_since = None
        self.index = 0
        self.plan = None
        self.clearance = None
        self.done = False

    def status(self):
        return dict(phase=self.phase, detail=self.detail, sides=list(self.sides),
                    clearance_recovery=self.clearance.status() if self.clearance else [])

    def _phase(self, phase, detail, now, timeout=8.0):
        self.phase, self.detail, self.deadline = phase, detail, now + timeout

    def tick(self, now):
        dt = min(.02, max(0., now - self.last_tick))
        self.last_tick = now
        if now > self.deadline:
            raise RuntimeError(f'{self.detail}超时；请释放硬件急停并检查仍存在的控制器/伺服故障')
        try:
            self._tick(now, dt)
            self.busy_since = None
        except CommandBusy:
            # A busy buffer means nothing was sent. Retry that transaction,
            # never re-send a motion already accepted by the SDK.
            if self.busy_since is None:
                self.busy_since = now
            if now - self.busy_since > 1.0:
                raise RuntimeError('回零时 SDK 发送缓冲持续忙，已停止')
        except ValueError as exc:
            if (self.phase == 'feedback' and not all(self.robot.frames.values())
                    and 'UDP feedback not advancing' in str(exc)):
                return  # Initial connection may not have received both lanes yet.
            raise

    def _tick(self, now, dt):
        raw = self.phase in ('feedback', 'idle_ack')
        q = self.robot.read(allow_faults=raw)
        if self.phase == 'feedback':
            # Freshness and finite values are mandatory even in ERROR state.
            self.request_frames = self.robot.frames.copy()
            self.robot.request_recovery_idle()
            self._phase('idle_ack', '回零：清错并等待双臂下使能确认', now)
            return
        if self.phase == 'idle_ack':
            if not all(self.robot.frames[s] != self.request_frames[s]
                       and self.robot.actual_states[s] == 0
                       and self.robot.feedback_status[s]['state']['err_code'] == 0
                       for s in self.models.arms):
                return
            if any(np.max(np.abs(v)) > .5 for v in self.robot.velocities.values()):
                return
            self.robot.check_servo_errors()
            self.robot.enabled_sides.clear()
            self.robot.pending_sides.clear()
            self.robot.stopping_sides.clear()
            self.clearance = RecoveryClearance(self.models, q, self.config['collision_margin_m'])
            validate_pose(self.models, q, self.config, hard_limits=True, collision_guard=self.clearance)
            # Clear an existing close-housing warning before moving the peer.
            # Channel order breaks ties when neither arm needs clearance recovery.
            self.sides = tuple(sorted(self.sides, key=lambda s: (self.clearance.priority(s), self.config['arm_channels'][s])))
            self._phase('plan', '回零：检查逐臂回零路径', now)
            return
        side = self.sides[self.index]
        if self.phase == 'plan':
            goals = {s: v.copy() for s, v in q.items()}
            goals[side] = np.zeros(7)
            self.plan = pose_plan(self.models, q, goals, (side,), self.config,
                                  hard_limits=True, validate=False, speed_limit=5.4,
                                  acceleration=5.0)
            self.checked = 0
            self.preflight_clearance = self.clearance.fork()
            self._phase('validate', f'回零：检查{side}臂路径', now, timeout=30.)
        elif self.phase == 'validate':
            if any(np.max(np.abs(q[s] - self.plan['start'][s])) > .1 for s in q):
                raise ValueError('回零预检期间机械臂位置改变，请保持静止后重试')
            u = self.checked / (self.plan['samples'] - 1)
            candidate = {s: self.plan['start'][s] + u * self.plan['delta'][s] for s in q}
            validate_pose(self.models, candidate, self.config, hard_limits=True,
                          collision_guard=self.preflight_clearance)
            self.preflight_clearance.advance(candidate)
            self.checked += 1
            if self.checked == self.plan['samples']:
                self._phase('hold', f'回零：写入{side}臂当前保持点', now)
        elif self.phase == 'hold':
            self.robot.stage_recovery_hold(side)
            self.request_frames = self.robot.frames.copy()
            self._phase('hold_ack', f'回零：确认{side}臂保持点', now)
        elif self.phase == 'hold_ack':
            if self.robot.frames[side] != self.request_frames[side]:
                if np.max(np.abs(q[side] - self.plan['start'][side])) > .1:
                    raise ValueError('回零使能前关节位置发生变化，请重试')
                self.robot.enable((side,))
                self._phase('enable_ack', f'回零：等待{side}臂位置模式', now)
        elif self.phase == 'enable_ack':
            if side in self.robot.enabled_sides:
                self._phase('moving', f'回零：{side}臂低速回零', now,
                            timeout=self.plan['duration'] + 5.)
        elif self.phase == 'moving':
            # Commit elapsed only after send acceptance, so buffer stalls cannot
            # skip ahead in the trajectory on the next iteration.
            elapsed = min(self.plan['duration'], self.plan['elapsed'] + dt)
            target = pose_at(self.plan, elapsed)
            for s in q:
                if s != side:
                    target[s] = q[s]
            middle = {s: (q[s] + target[s]) / 2 for s in q}
            for candidate in (q, middle, target):
                validate_pose(self.models, candidate, self.config, hard_limits=True,
                              collision_guard=self.clearance)
            self.robot.command({side: target[side]})
            # Advance from feedback, never a future command the arm has not
            # reached. Preflight uses an independent guard so it cannot cause
            # feedback to be rejected against a future clearance floor.
            self.clearance.advance(q)
            self.plan['elapsed'] = elapsed
            if (elapsed >= self.plan['duration'] and np.max(np.abs(q[side])) <= .15
                    and np.max(np.abs(self.robot.velocities[side])) <= .5):
                self._phase('disable', f'回零：{side}臂到位，正在下使能', now)
        elif self.phase == 'disable':
            self.robot.stop((side,))
            self._phase('disable_ack', f'回零：等待{side}臂下使能确认', now)
        elif self.phase == 'disable_ack':
            if side not in self.robot.stopping_sides and self.robot.actual_states[side] == 0:
                self.index += 1
                if self.index == len(self.sides):
                    self.done = True
                    self.detail = '所选机械臂已逐臂回零并下使能'
                else:
                    self._phase('plan', '回零：检查下一臂路径', now)
