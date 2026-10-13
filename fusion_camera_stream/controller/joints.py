"""Read existing FUSION DCSS, never write memory, send UDP, or call robot SDK.

Verified against FXPSI InitRtUDP (1430-byte FX frame), libFXShm (+8 header),
and SDK_PYTHON/fx_robot.py DCSS layout: size1428, m_Out offset760, stride312.
This layout is specific to this controller firmware; deployment pins its hash.
"""
from __future__ import print_function
import math
import mmap
import os
import struct

NAMES = ['left_joint_%d' % i for i in range(1, 8)] + ['right_joint_%d' % i for i in range(1, 8)]

def parse(data):
    if len(data) != 1430 or data[:2] != b'FX':
        raise ValueError('DCSS signature or size mismatch')
    states = struct.unpack_from('<6i', data, 2)
    serial, pos, vel, torque = [], [], [], []
    for arm in range(2):
        base = 762 + 312 * arm
        serial.append(struct.unpack_from('<i', data, base)[0])
        pos.extend(struct.unpack_from('<7f', data, base+4))
        vel.extend(struct.unpack_from('<7f', data, base+32))
        torque.extend(struct.unpack_from('<7f', data, base+144))
    if any(s < 0 or s >= 1000000 for s in serial):
        raise ValueError('Invalid DCSS frame serial')
    if any(math.isnan(v) or math.isinf(v) for v in pos+vel+torque):
        raise ValueError('Invalid DCSS joint feedback')
    if any(abs(v) > 720 for v in pos):
        raise ValueError('DCSS position outside parser bounds')
    return {'frame_serial': serial, 'position_deg': pos, 'velocity_deg_s': vel,
            'torque_nm': torque, 'arm_state': list(states), 'joint_names': NAMES}

class JointReader(object):
    def __init__(self, clock):
        self.clock = clock
        fd = os.open('/dev/FXDcss', os.O_RDONLY)
        try:
            self.memory = mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        finally:
            os.close(fd)
        try:
            if struct.unpack('<I', self.memory[:4])[0] != 4096:
                raise ValueError('Unknown DCSS mapping size')
            self.sample()
        except Exception:
            self.close()
            raise

    def sample(self):
        for attempt in range(4):
            begin = int(self.clock()*1e9)
            first = self.memory[8:1438]
            second = self.memory[8:1438]
            end = int(self.clock()*1e9)
            if first == second:
                result = parse(first)
                # Different arm counters can occur while the producer updates.
                if result['frame_serial'][0] != result['frame_serial'][1]:
                    continue
                result.update(timestamp_ns=(begin+end)//2, read_span_ns=end-begin,
                              consistency_attempts=attempt+1,
                              timestamp_kind='controller_monotonic_shared_state_read')
                return result
        raise RuntimeError('DCSS changed during bounded snapshot reads')

    def close(self):
        self.memory.close()
