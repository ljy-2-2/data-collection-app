"""Python 2.7-compatible, exact servo-log exceptions for teleop observation."""
import hashlib
import re

POLICY = 'known-servo-transitions-v2'
SERVO_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+'
    b'(?:Mot\\[[01]\\]\\[[0-6]\\] process (?:enable|disable|change to position mode)'
    b'|_FX_ServoFsmWaitPositionMode set ctrl\\.b15=0, ctrl\\.b14=0'
    b'|_FX_ServoFsmShutDown 0x8006'
    b'|_FX_ServoFsmSwitchOnAndEnableOperation 0x800f'
    b'|_FX_ServoFsmEnableOperation 0x800f'
    b'|_FX_ServoFsmDisableOperation 0x8007)\\s*$'
)


def dmesg_fingerprint(raw):
    lines = raw.splitlines(True)
    retained = [line for line in lines if not SERVO_LINE.match(line)]
    return {
        'dmesg_sha256': hashlib.sha256(raw).hexdigest(),
        'dmesg_nonservo_sha256': hashlib.sha256(b''.join(retained)).hexdigest(),
        'dmesg_servo_lines': len(lines) - len(retained),
        'dmesg_policy': POLICY,
    }


def audit_changed(before, after, key, mode):
    if before[key] == after[key]:
        return False
    if (key == 'dmesg_sha256' and mode == 'teleop_observation'
            and before.get('dmesg_policy') == after.get('dmesg_policy') == POLICY
            and before.get('dmesg_nonservo_sha256')
            and before['dmesg_nonservo_sha256'] == after.get('dmesg_nonservo_sha256')):
        return False
    return True
