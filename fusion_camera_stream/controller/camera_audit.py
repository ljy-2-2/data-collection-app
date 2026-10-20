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


HOTPLUG_POLICY = 'camera-ports-enumeration-v1'
# Normal enumeration only, on known camera ports. No xHCI errors or USB resets.
CAMERA_HOTPLUG_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+usb 1-[124]: '
    b'(?:USB disconnect, device number [0-9]+'
    b'|new high-speed USB device number [0-9]+ using xhci_hcd'
    b'|New USB device found, idVendor=(?:32e4, idProduct=9230|0bda, idProduct=5883)'
    b'|New USB device strings: Mfr=[0-9]+, Product=[0-9]+, SerialNumber=0'
    b'|(?:Product|Manufacturer): HD USB Camera)\\s*$'
)


def dmesg_fingerprint(raw):
    lines = raw.splitlines(True)
    retained = [line for line in lines if not SERVO_LINE.match(line)]
    return {
        'dmesg_sha256': hashlib.sha256(raw).hexdigest(),
        'dmesg_nonservo_sha256': hashlib.sha256(b''.join(retained)).hexdigest(),
        'dmesg_servo_lines': len(lines) - len(retained),
        'dmesg_policy': POLICY,
        'camera_hotplug_policy': HOTPLUG_POLICY,
        'dmesg_nonservo_noncamera_sha256': hashlib.sha256(
            b''.join(line for line in retained if not CAMERA_HOTPLUG_LINE.match(line))).hexdigest(),
        'dmesg_noncamera_sha256': hashlib.sha256(
            b''.join(line for line in lines if not CAMERA_HOTPLUG_LINE.match(line))).hexdigest(),
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


def camera_hotplug_only(before, after, mode):
    """For a later explicit retry, never to mark a failed stream valid."""
    key = ('dmesg_nonservo_noncamera_sha256' if mode == 'teleop_observation'
           else 'dmesg_noncamera_sha256')
    return (before.get('camera_hotplug_policy') == after.get('camera_hotplug_policy') == HOTPLUG_POLICY
            and bool(before.get(key)) and before[key] == after.get(key))
