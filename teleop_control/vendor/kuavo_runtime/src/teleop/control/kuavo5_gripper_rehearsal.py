"""Deterministic in-memory rehearsal of the actual gripper session/codec.

No sockets, discovery, robot imports, real clocks or physical transports.
Fake feedback echoes accepted targets instantly: NOT a motor dynamics model.
Candidate addresses are real proposals; ALL travel/current values are fictional.
"""
import struct

from teleop.contracts.xr import ControllerButtons, ControllerState, Pose, XRFrame
from teleop.control.kuavo5_gripper_teleop import KuavoGripperTeleop
from teleop.robots.damiao_gripper import DISABLE, ENABLE, DamiaoGripperDriver, MotorProfile
from teleop.robots.kuavo5_gripper_candidate import assess_candidate
from teleop.robots.kuavo5_live_protocol import InputLease, make_input_packet


class FakeRig:
    def __init__(self, candidate):
        report = assess_candidate(candidate)
        self.routes = report["routes"]
        self.now, self.sequence = 1_000_000_000, 0
        self.commands, self.busy_sides = [], set()
        self.profiles = {
            side: MotorProfile(route["bus"], route["motor_id"], route["feedback_can_id"],
                               -.5, .5, .02, 1., 10., .1, 2., 10., 12.5, 30., 10.)
            for side, route in self.routes.items()
        }
        self.motors = {side: dict(position=0., state=0, temperature=25) for side in self.routes}
        # The proposal has already checked the available partial inventory.
        # Empty inventories below explicitly describe the isolated FAKE buses.
        self.driver = DamiaoGripperDriver(self.profiles,
            occupied_ids={p.bus: set() for p in self.profiles.values()}, submit=self.submit)
        self.session = KuavoGripperTeleop(self.driver, clock=lambda: self.now)
        self.lease = InputLease()

    def submit(self, bus, frames, deadline):
        side, = [side for side, profile in self.profiles.items() if profile.bus == bus]
        if self.now >= deadline or side in self.busy_sides:
            return False
        for can_id, payload in frames:
            if can_id != self.routes[side]["control_can_id"] or len(payload) != 8:
                raise ValueError("wrong fake route or command length")
            if payload == ENABLE:
                kind = "enable"
                self.motors[side]["state"] = 1
            elif payload == DISABLE:
                kind = "disable"
                self.motors[side]["state"] = 0
            else:
                kind = "position"
                self.motors[side]["position"] = struct.unpack("<fHH", payload)[0]
            self.commands.append(dict(side=side, bus=bus, can_id=can_id,
                                      kind=kind, time_ns=self.now, data_hex=payload.hex()))
        return True

    def step(self, left=0., right=0., *, deliver_input=True, omit_feedback=(), allowed=True):
        self.now += 10_000_000
        self.sequence += 1
        for side, profile in self.profiles.items():
            if side in omit_feedback:
                continue
            motor = self.motors[side]
            code = round((motor["position"] + profile.pmax) * 65535 / (2 * profile.pmax))
            raw = bytes(((motor["state"] << 4) | profile.motor_id, code >> 8, code & 255,
                         0x80, 0x08, 0, motor["temperature"], motor["temperature"]))
            self.driver.receive(profile.bus, profile.master_id, raw, received_ns=self.now)
        if deliver_input:
            pose = Pose((0., 0., 0.), (0., 0., 0., 1.))
            def hand(trigger):
                return ControllerState(pose, (0., 0.), 0., trigger, ControllerButtons())
            # Separate fictional PC/robot clock origins exercise the shared lease.
            pc_now = self.now + 80_000_000_000
            frame = XRFrame(self.sequence, self.sequence + 1, pc_now, False, pose,
                            hand(left), hand(right), source="synthetic-gripper-rehearsal")
            packet = make_input_packet(self.lease.challenge(self.now), frame, now_ns=pc_now)
            self.session.observe(self.lease.accept(packet, now_ns=self.now))
        return self.session.tick(allowed=allowed)

    def run(self, ticks, left, right, **kwargs):
        for _ in range(ticks):
            report = self.step(left, right, **kwargs)
        return report

    def activate(self, left=1., right=1.):
        self.step(0., 0.)
        return self.run(8, left, right)


def rehearse(candidate):
    candidate_report = assess_candidate(candidate)
    checks, rigs = [], []
    def rig():
        value = FakeRig(candidate)
        rigs.append(value)
        return value
    def check(name, expected, observed):
        checks.append(dict(name=name, passed=observed == expected, expected=expected, observed=observed))
    def states(report):
        return {side: motor["state"] for side, motor in report["motors"].items()}

    held = rig()
    report = held.run(5, 1., 1.)
    check("held_at_start_requires_release", {"left": "WAIT_RELEASE", "right": "WAIT_RELEASE"}, states(report))
    check("held_at_start_no_fake_commands", 0, len(held.commands))

    for side in ("left", "right"):
        single = rig()
        left, right = (1., 0.) if side == "left" else (0., 1.)
        single.activate(left, right)
        report = single.run(30, left, right)
        check(side + "_only_activation", {s: "ACTIVE" if s == side else "READY" for s in ("left", "right")}, states(report))
        observed = sorted({(cmd["side"], cmd["bus"], cmd["can_id"]) for cmd in single.commands})
        route = candidate_report["routes"][side]
        check(side + "_only_route", [(side, route["bus"], route["control_can_id"])], observed)

    both = rig()
    both.activate(.8, .3)
    report = both.run(30, .8, .3)
    check("both_active_with_arm_grips_released", {"left": "ACTIVE", "right": "ACTIVE"}, states(report))
    check("independent_trigger_targets", True,
          report["motors"]["left"]["target_rad"] > 0 > report["motors"]["right"]["target_rad"])
    bounded = all(p.bounds[0] <= struct.unpack("<fHH", bytes.fromhex(cmd["data_hex"]))[0] <= p.bounds[1]
                  for cmd in both.commands if cmd["kind"] == "position"
                  for p in (both.profiles[cmd["side"]],))
    check("fake_targets_stay_inside_fictional_travel", True, bounded)

    gap = rig()
    gap.activate()
    report = gap.run(12, 1., 1., deliver_input=False)
    check("input_timeout_returns_to_wait_release", {"left": "WAIT_RELEASE", "right": "WAIT_RELEASE"}, states(report))
    check("input_timeout_revokes_fake_motors", {"left": 0, "right": 0},
          {side: motor["state"] for side, motor in gap.motors.items()})
    before = len(gap.commands)
    report = gap.run(3, 1., 1.)
    check("recovery_while_held_does_not_retake", {"left": "WAIT_RELEASE", "right": "WAIT_RELEASE"}, states(report))
    check("recovery_while_held_no_fake_commands", before, len(gap.commands))
    check("release_after_recovery_is_ready", {"left": "READY", "right": "READY"}, states(gap.step()))
    check("press_after_recovery_reactivates", {"left": "ACTIVE", "right": "ACTIVE"}, states(gap.run(3, .8, .3)))

    missing = rig()
    missing.activate()
    report = missing.run(20, .8, .3, omit_feedback=("left",))
    check("left_feedback_timeout_isolated", {"left": "FAULT", "right": "ACTIVE"}, states(report))
    check("left_feedback_timeout_reason", True, "stale" in report["motors"]["left"]["reason"])

    hot = rig()
    hot.activate()
    hot.motors["left"]["temperature"] = 65
    report = hot.step(.8, .3)
    check("left_overtemperature_isolated", {"left": "FAULT", "right": "ACTIVE"}, states(report))
    check("left_overtemperature_reason", "motor overtemperature", report["motors"]["left"]["reason"])

    busy = rig()
    busy.activate()
    before = busy.session.status()["motors"]["left"]["target_rad"]
    busy.busy_sides.add("left")
    report = busy.run(3, 1., 1.)
    check("busy_side_does_not_advance_unsent_target", before, report["motors"]["left"]["target_rad"])
    check("busy_side_defers_without_fault", True,
          report["motors"]["left"]["deferred_sends"] >= 3 and report["motors"]["right"]["state"] == "ACTIVE")

    guarded = rig()
    guarded.activate()
    check("session_guard_revokes_both", {"left": "WAIT_RELEASE", "right": "WAIT_RELEASE"},
          states(guarded.step(1., 1., allowed=False)))
    for value in rigs:
        value.busy_sides.clear()
        value.session.close()
    check("closing_all_sessions_revokes_fake_motors", True,
          all(motor["state"] == 0 for value in rigs for motor in value.motors.values()))
    check("closing_all_sessions_leaves_no_pending_disable", True,
          all(not value.session.status()["disable_pending"] for value in rigs))

    passed = all(item["passed"] for item in checks)
    return dict(event="KUAVO_GRIPPERS_SYNTHETIC_OFFLINE", status="PASS" if passed else "FAIL",
                source="synthetic", hardware_io=False, hardware_connected=False,
                physical_robot_command_sent=False, can_frames_sent=0,
                physical_motor_communication_verified=False,
                physical_execution_verified=False, actual_motor_dynamics_simulated=False,
                measured_samples_are_synthetic=True, profiles_are_synthetic=True,
                route_ids_are_candidate=True, routes=candidate_report["routes"],
                arm_grips_released=True, fictional_travel_rad=[-.5, .5],
                fake_control_period_ms=10, rate_is_not_hardware_measurement=True,
                accepted_fake_can_frames=sum(len(value.commands) for value in rigs),
                checks_passed=sum(item["passed"] for item in checks), checks_total=len(checks),
                checks=checks, hardware_blockers=candidate_report["hardware_blockers"])
