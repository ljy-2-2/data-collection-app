"""Pure temporal orientation analysis for center-camera video frames."""

from __future__ import annotations

import numpy as np


def choose_next_vertical_orientation(
    previous: np.ndarray, current: np.ndarray, prior_state: bool
) -> bool:
    """Select the current raw frame's orientation against normalized previous."""
    normal_error = float(np.abs(current.astype(np.int16) - previous).mean())
    flipped_error = float(np.abs(current[::-1].astype(np.int16) - previous).mean())
    if flipped_error < 12.0 and flipped_error * 4.0 + 4.0 < normal_error:
        return True
    if normal_error < 12.0 and normal_error * 4.0 + 4.0 < flipped_error:
        return False
    return prior_state


def choose_vertical_orientations(frames: np.ndarray) -> tuple[list[bool], int]:
    """Choose a temporally consistent orientation for low-resolution frames.

    The returned booleans are relative to the first frame: ``True`` means the
    corresponding source frame must be flipped to match it. A switch is only
    accepted when the mirrored candidate is dramatically closer, which is the
    signature produced by duplicate raw/corrected publishers. Ordinary motion
    or an episode cut remains in the previous state.
    """
    if len(frames) == 0:
        return [], 0
    states = [False]
    previous = frames[0].astype(np.int16)
    switches = 0
    for frame in frames[1:]:
        current = frame.astype(np.int16)
        prior_state = states[-1]
        state = choose_next_vertical_orientation(previous, current, prior_state)
        if state != prior_state:
            switches += 1
        states.append(state)
        previous = current[::-1] if state else current
    return states, switches
