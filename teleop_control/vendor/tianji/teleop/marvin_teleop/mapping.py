"""Same-facing mapping, derived from Marvin installation and native VR axes.

See COORDINATES.md for source documents and the full transform chain.
The XR/operator rotation follows test-teleop/retarget.py. Robot mounting,
encoder axes and tool geometry remain independently derived from Marvin.
"""

import numpy as np


# OpenXR: +X right, +Y up, -Z forward. Robot: +X forward, +Y left, +Z up.
ROBOT_FROM_XR = np.array([[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]])


def installed_base_from_nominal(base, orientation):
    """Keep manufacturer mounting; physical corrections require measured registration.

    An operator standing behind the robot does not rotate either arm base.
    Retire the empirical Y half-turn: it reverses both forward and gravity.
    """
    if orientation != "cad":
        raise ValueError("mount_orientation: use cad and measured installation_calibration")
    return np.asarray(base, dtype=float).copy()


def unit(vector):
    vector = np.asarray(vector, dtype=float)
    length = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or length < 1e-8:
        raise ValueError("coordinate calibration has a degenerate axis")
    return vector / length


def robot_world_from_mount(bases, gravity):
    """Return T_world_mount, keeping the fixture CAD origin.

    L/R are the robot's anatomical sides (manual figure 2-6).
    GRV is gravity expressed in each vendor base frame. The two fixed
    Joint0 origins express these frames in the fixture URDF.
    """
    up_by_side = {s: unit(-bases[s][:3, :3] @ np.asarray(gravity[s])) for s in bases}
    if np.linalg.norm(up_by_side["left"] - up_by_side["right"]) > 1e-4:
        raise ValueError("left/right mounting rotations and vendor gravity disagree")
    up = unit(up_by_side["left"] + up_by_side["right"])
    left = unit(bases["left"][:3, 3] - bases["right"][:3, 3])
    if abs(left @ up) > 1e-3:
        raise ValueError("Marvin shoulder baseline is not horizontal")
    left = unit(left - up * (left @ up))
    forward = unit(np.cross(left, up))
    # Independent orientation check: both installed base +X axes point forward.
    if any(np.linalg.norm(t[:3, 0] - forward) > 1e-3 for t in bases.values()):
        raise ValueError("Marvin base +X axes disagree with anatomical forward")
    result = np.eye(4)
    result[:3, :3] = np.stack((forward, left, up))
    return result


def behind_operator_convention():
    """One explicit anatomical convention for live IK, records and playback.

    The operator is behind the robot, looking at its back. Both look along +X;
    a rear camera therefore has screen-right = -Y, never +Y.
    Positions here describe the relationship, not a measured room transform.
    """
    return {
        "id": "operator_behind_robot_v1",
        "operator_position": "behind_robot",
        "forward_world": [1.0, 0.0, 0.0],
        "left_world": [0.0, 1.0, 0.0],
        "up_world": [0.0, 0.0, 1.0],
        "rear_camera_side_world": [-1.0, 0.0, 0.0],
        "rear_screen_right_world": [0.0, -1.0, 0.0],
        "hand_to_arm": {"left": "left", "right": "right"},
        "description": "操作者站在机器人背后，脸朝机器人背部；双方看向同一个前方",
    }


def robot_from_behind_operator(arms):
    """Validate the nominal CAD convention before any measured registration.

    Corrected physical bases need not have local +X aligned with world +X;
    their measured rotations are applied later without redefining body axes.
    """
    convention = behind_operator_convention()
    expected = np.column_stack(
        [convention[k] for k in ("forward_world", "left_world", "up_world")]
    )
    left = unit(arms["left"].base[:3, 3] - arms["right"].base[:3, 3])
    for arm in arms.values():
        forward = unit(arm.base[:3, 0])
        up = unit(-arm.base[:3, :3] @ arm.gravity)
        if not np.allclose(np.column_stack((forward, left, up)), expected, atol=1e-6):
            raise ValueError("机械臂安装坐标与背后同向基准不一致，不能建立遥操映射")
    return expected


def operator_yaw_frame(head):
    """Operator axes in tracking world, using the reference yaw convention."""
    head = np.asarray(head, dtype=float)
    if head.shape != (4, 4) or not np.isfinite(head).all():
        raise ValueError("invalid headset pose for behind-robot calibration")
    up = np.array([0., 1., 0.])
    forward = head[:3, :3] @ np.array([0., 0., -1.])
    forward -= up * (forward @ up)
    if np.linalg.norm(forward) < 0.5:
        raise ValueError("站在机器人背后，与机器人看向共同前方并平视，再接管校准")
    backward = -unit(forward)
    return np.column_stack((unit(np.cross(up, backward)), up, backward))


def same_facing_map(head, robot_from_operator=None):
    """Map native VR tracking coordinates to robot forward/left/up.

    Native pose contract: right-handed, +X right, +Y up, -Z forward.
    At calibration the operator looks in the same direction as the robot.
    Horizontal heading only; headset pitch/roll never tilt the robot world.
    Controller captures this rotation on the first clutch of a VR session.
    Both arms share it across re-clutches; a new VR session recalibrates it.
    """
    yaw_frame = operator_yaw_frame(head)
    robot_from_operator = (
        np.eye(3)
        if robot_from_operator is None
        else np.asarray(robot_from_operator, dtype=float)
    )
    if (
        robot_from_operator.shape != (3, 3)
        or not np.isfinite(robot_from_operator).all()
        or not np.allclose(
            robot_from_operator.T @ robot_from_operator, np.eye(3), atol=1e-8
        )
        or not np.isclose(np.linalg.det(robot_from_operator), 1, atol=1e-8)
    ):
        raise ValueError("operator-to-robot axes must form a proper rotation")
    # Same reference chain for both translation and rotation conjugation:
    # tracking world -> horizontal operator frame -> robot world.
    # Both arms share this heading for the session; no per-hand recalibration.
    return robot_from_operator @ ROBOT_FROM_XR @ yaw_frame.T
