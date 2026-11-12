"""Factory poses in manufacturer joint degrees, independent of user records."""

WORK_POSE_ID = 'tianji-work'


def builtin_poses(robot):
    # Fresh values prevent a UI state or saved record from changing the preset.
    if robot != 'tianji':
        return []
    return [dict(id=WORK_POSE_ID, name='工作姿态（内置）', targets=dict(
        left=[90, -90, -90, -105, 135, 0, 0],
        right=[-90, -90, 90, -105, -135, 0, 0]))]


def protected_pose(robot, pose_id):
    return pose_id == 'zero' or any(p['id'] == pose_id for p in builtin_poses(robot))
