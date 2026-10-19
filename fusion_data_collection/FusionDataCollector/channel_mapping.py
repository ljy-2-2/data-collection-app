"""Convert the unchanged controller A/B stream to anatomical arm ordering."""


def anatomical_joint_sample(sample, channels):
    if channels is None:
        return sample
    if (not isinstance(channels, dict) or set(channels) != {'left', 'right'}
            or set(channels.values()) != {'A', 'B'}):
        raise ValueError('Invalid controller arm channel mapping')
    expected = [side+'_joint_'+str(joint) for side in ('left', 'right') for joint in range(1, 8)]
    if sample.get('joint_names') != expected:
        raise ValueError('Unexpected controller A/B joint names')
    if 'anatomical_arm_channels' in sample:
        raise ValueError('Joint channel mapping must be applied only once')
    indices = [0 if channels[side] == 'A' else 1 for side in ('left', 'right')]
    result = dict(sample, anatomical_arm_channels=dict(channels))
    for key, width in (('position_deg', 7), ('velocity_deg_s', 7), ('torque_nm', 7),
                       ('frame_serial', 1), ('arm_state', 3)):
        if key not in sample and key == 'arm_state':
            continue
        values = sample[key]
        if len(values) != width*2:
            raise ValueError('Unexpected controller vector size: '+key)
        result[key] = [value for index in indices for value in values[index*width:(index+1)*width]]
    return result
