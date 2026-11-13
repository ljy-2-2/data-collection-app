"""Factory poses in manufacturer joint degrees, independent of user records."""

WORK_POSE_ID = 'tianji-work'


def builtin_poses(robot):
    # Fresh values prevent a UI state or saved record from changing the preset.
    if robot != 'tianji':
        return []
    # Approved plan A (2026-11-13): preserve J1-J3, pitch TCP +X down 30 deg
    # without additional roll. TCP height is ~98.53 cm above the CAD stand's
    # lowest point; this is a model reference, not measured floor calibration.
    return [dict(id=WORK_POSE_ID, name='工作姿态（内置）', targets=dict(
        left=[90, -90, -90, -130, 108.881721, -41.641143, -62.763687],
        right=[-90, -90, 90, -130, -108.881721, -41.641143, 62.763687]))]


def protected_pose(robot, pose_id):
    return pose_id == 'zero' or any(p['id'] == pose_id for p in builtin_poses(robot))
