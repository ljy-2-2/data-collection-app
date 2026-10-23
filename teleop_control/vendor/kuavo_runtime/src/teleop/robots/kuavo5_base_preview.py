"""Pure /cmd_vel payload encoding. No ROS publisher or control side effects."""
from teleop.contracts.base_motion import BaseVelocityIntent


def encode_base_twist(intent):
    if not isinstance(intent, BaseVelocityIntent):
        raise ValueError('expected BaseVelocityIntent')
    return {
        'linear': {'x': intent.forward_m_s, 'y': intent.left_m_s, 'z': 0.},
        'angular': {'x': 0., 'y': 0., 'z': intent.yaw_rad_s},
    }
