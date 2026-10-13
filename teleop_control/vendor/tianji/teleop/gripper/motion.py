"""Bounded position trajectory; feedback is never modified or interpolated."""
import math


def ramp(position, velocity, goal, dt, speed, accel, lower, upper):
    if dt <= 0:
        return position, velocity
    goal = max(lower, min(upper, goal))

    def braking(distance):
        # Reserve one full integration step in addition to stopping distance.
        step = accel * dt
        return max(0.0, 2 * accel * max(0.0, distance) /
                   (math.sqrt(step * step + 2 * accel * max(0.0, distance)) + step))

    error = goal - position
    wanted = math.copysign(min(speed, braking(abs(error))), error)
    velocity = max(velocity - accel * dt, min(velocity + accel * dt, wanted))
    velocity = max(-braking(position - lower), min(braking(upper - position), velocity))
    target = max(lower, min(upper, position + velocity * dt))
    if abs(goal - target) < 1e-9 and abs(velocity) <= accel * dt:
        return goal, 0.0
    return target, velocity
