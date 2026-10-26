"""Acceleration-bounded steps with a stopping envelope inside Marvin limits."""

import numpy as np


def advance(arm, q, desired, velocity, dt, speed, accel, goal_velocity=None):
    # Keep a common path parameter while respecting each motor's own limit.
    accel = np.broadcast_to(np.asarray(accel, dtype=float), (7,))
    if not np.isfinite(accel).all() or np.any(accel <= 0):
        raise ValueError("invalid joint acceleration limits")
    accel = np.minimum(accel, arm.acceleration_limits)
    # Subtraction of sent double-precision angles at an exact limit can invent
    # ~1e-12 deg/s of outward velocity. Remove only sub-nanodegree/s roundoff;
    # never relax the physical joint domain or discard meaningful momentum.
    velocity = np.where(np.abs(velocity) < 1e-9, 0.0, velocity)
    error = desired - q
    # Approach a nearby goal at a velocity that can stop there. This avoids the
    # old acceleration filter coasting past the IK goal and wrist limits.
    # One time scale for the whole IK step. Independent component clipping
    # changes J*dq and can reverse the flange's motion, even from rest.
    distance = float(np.max(np.abs(error)))
    terminal_speed = 0.
    if goal_velocity is not None and distance > 1e-12:
        # A moving hand target is not a stationary stop point. Retain its
        # velocity along the joint step instead of braking toward zero at
        # every sample. The complete hardware stopping check below still uses
        # the actual commanded velocity and zero terminal speed.
        terminal_speed = float(np.clip(
            np.dot(goal_velocity, error) * distance / np.dot(error, error), 0., speed))
    direction = error / distance if distance > 1e-12 else np.zeros(7)
    moving = np.abs(direction) > 1e-12
    path_accel = float(np.min(accel[moving] / np.abs(direction[moving]))) if np.any(moving) else float(np.min(accel))
    stopping_speed = np.sqrt((path_accel * dt) ** 2 + terminal_speed**2 + 2 * path_accel * distance) - path_accel * dt
    speed_along_step = min(distance / dt, speed, stopping_speed)
    wanted = error * (speed_along_step / distance) if distance > 1e-12 else np.zeros(7)
    velocity_change = wanted - velocity
    blend = min(1.0, float(np.min(accel * dt / np.maximum(1e-12, np.abs(velocity_change)))))
    proposed = velocity + blend * velocity_change
    proposed = np.where(np.abs(proposed) < 1e-9, 0.0, proposed)

    def positions(v):
        next_q = q + v * dt
        # Match the velocity reconstructed by Controller from the sent angles;
        # at an exact wrist boundary, floating-point subtraction matters.
        sent_velocity = (next_q - q) / dt
        sent_velocity = np.where(np.abs(sent_velocity) < 1e-9, 0.0, sent_velocity)
        # All axes brake together in max(|v_i|/accel_i) seconds. For this
        # robot the intersected joint/wrist domain is convex (checked at load).
        stop_q = next_q + sent_velocity * np.max(np.abs(sent_velocity) / accel) / 2
        return next_q, stop_q

    def checked(v):
        original = v.copy()
        for _ in range(4):
            next_q, stop_q = positions(v)
            if arm.valid_joints(next_q) and arm.valid_joints(stop_q):
                return v
            faces, bounds = arm.joint_halfspaces()
            violation = np.maximum(faces @ next_q - bounds, faces @ stop_q - bounds)
            worst = int(np.argmax(violation))
            if violation[worst] > 1e-9:
                return None  # A real loss of stopping feasibility must still stop.
            # Coupled wrist faces can cancel to zero while their components
            # move at several deg/s. Component zeroing alone cannot fix this.
            # Move the numeric result just INSIDE the same domain, then perform
            # the original strict position and stopping checks again.
            normal = faces[worst]
            v = v - normal * (
                (max(0.0, violation[worst]) + 1e-10) / (dt * (normal @ normal))
            )
            if (
                np.max(np.abs(v - original)) > 1e-7
                or np.max(np.abs(v)) > speed + 1e-9
                or np.any(np.abs(v - velocity) > accel * dt + 1e-9)
            ):
                return None
        return None

    constrained = False
    valid_proposal = checked(proposed)
    if valid_proposal is None:
        constrained = True
        # Start braking before crossing the boundary, keep the clutch engaged.
        factor = max(0.0, 1 - dt / max(1e-12, np.max(np.abs(velocity) / accel)))
        proposed = velocity * factor
        valid_proposal = checked(proposed)
        if valid_proposal is None:
            # Only reachable with an externally replaced/inconsistent integrator.
            raise ValueError("no bounded stopping path inside joint limits")
    return q + valid_proposal * dt, constrained
