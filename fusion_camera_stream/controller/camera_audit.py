"""Python 2.7-compatible, exact servo-log exceptions for teleop observation."""
import hashlib
import re

POLICY = 'known-servo-transitions-v3'
TIMING_POLICY = 'single-hrtimer-under-100us-with-rt-guard-v1'
MAX_TIMER_NOTICE_NS = 100000
TIMER_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+hrtimer: interrupt took ([0-9]{1,20}) ns\\s*$')
SERVO_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+'
    b'(?:Mot\\[[01]\\]\\[[0-6]\\] process (?:enable|disable|reset|change to position mode)'
    b'|_FX_ServoFsmWaitPositionMode set ctrl\\.b15=0, ctrl\\.b14=0'
    b'|_FX_ServoFsmShutDown 0x8006'
    b'|_FX_ServoFsmSwitchOnAndEnableOperation 0x800f'
    b'|_FX_ServoFsmEnableOperation 0x800f'
    b'|_FX_ServoFsmDisableOperation 0x8007'
    b'|_FX_ServoFsmFaultResetRising (?:0x80|0x8087)'
    b'|_FX_ServoFsmFaultResetFalling (?:0x0|0x8007))\\s*$'
)


HOTPLUG_POLICY = 'camera-ports-enumeration-v3-zed-hid'
# Normal enumeration only, on known camera ports. No xHCI errors or USB resets.
CAMERA_HOTPLUG_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+usb 1-[124]: '
    b'(?:USB disconnect, device number [0-9]+'
    b'|new high-speed USB device number [0-9]+ using xhci_hcd'
    b'|New USB device found, idVendor=(?:32e4, idProduct=9230|0bda, idProduct=5883)'
    b'|New USB device strings: Mfr=[0-9]+, Product=[0-9]+, SerialNumber=0'
    b'|(?:Product|Manufacturer): HD USB Camera)\\s*$'
)
ZED_HOTPLUG_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+usb 2-1: '
    b'(?:USB disconnect, device number [0-9]+'
    b'|new SuperSpeed USB device number [0-9]+ using xhci_hcd'
    b'|New USB device found, idVendor=2b03, idProduct=f682'
    b'|New USB device strings: Mfr=1, Product=2, SerialNumber=0'
    b'|Product: ZED-M|Manufacturer: Technologies, Inc\\.)\\s*$'
)
ZED_HID_HOTPLUG_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+usb 1-1: '
    b'(?:new full-speed USB device number [0-9]+ using xhci_hcd'
    b'|New USB device found, idVendor=2b03, idProduct=f681'
    b'|New USB device strings: Mfr=1, Product=2, SerialNumber=3'
    b'|Product: ZED-M Hid Device|Manufacturer: STEREOLABS'
    b'|SerialNumber: [0-9]+)\\s*$'
)
ZED_HID_ATTACH_LINE = re.compile(
    b'^\\[\\s*[0-9]+\\.[0-9]+\\]\\s+'
    b'hid-generic 0003:2B03:F681\\.[0-9A-Fa-f]{4}: '
    b'hiddev[0-9]+,hidraw[0-9]+: USB HID v1\\.11 Device '
    b'\\[STEREOLABS ZED-M Hid Device\\] on usb-0000:00:14\\.0-1/input0\\s*$'
)


def camera_enumeration_line(line):
    # ZED Mini has separate USB2 HID and USB3 video devices. A normal
    # power cycle enumerates both; neither is a kernel/USB error exemption.
    return (CAMERA_HOTPLUG_LINE.match(line) or ZED_HOTPLUG_LINE.match(line)
            or ZED_HID_HOTPLUG_LINE.match(line) or ZED_HID_ATTACH_LINE.match(line))


def dmesg_fingerprint(raw):
    lines = raw.splitlines(True)
    retained = [line for line in lines if not SERVO_LINE.match(line)]
    return {
        'dmesg_sha256': hashlib.sha256(raw).hexdigest(),
        'dmesg_nonservo_sha256': hashlib.sha256(b''.join(retained)).hexdigest(),
        'dmesg_servo_lines': len(lines) - len(retained),
        'dmesg_policy': POLICY,
        'timing_notice_policy': TIMING_POLICY,
        'timing_notice_lines': [line.decode('ascii').strip() for line in retained if TIMER_LINE.match(line)],
        'dmesg_nonservo_nontimer_sha256': hashlib.sha256(
            b''.join(line for line in retained if not TIMER_LINE.match(line))).hexdigest(),
        'camera_hotplug_policy': HOTPLUG_POLICY,
        'dmesg_nonservo_noncamera_sha256': hashlib.sha256(
            b''.join(line for line in retained if not camera_enumeration_line(line))).hexdigest(),
        'dmesg_noncamera_sha256': hashlib.sha256(
            b''.join(line for line in lines if not camera_enumeration_line(line))).hexdigest(),
    }


def timing_notice_warnings(before, after, mode):
    """Classify one bounded notice. Caller must independently verify RT invariants."""
    if mode != 'teleop_observation': return []
    if before.get('timing_notice_policy') != TIMING_POLICY or after.get('timing_notice_policy') != TIMING_POLICY: return []
    key = 'dmesg_nonservo_nontimer_sha256'
    if not before.get(key) or before[key] != after.get(key): return []
    old, new = before.get('timing_notice_lines'), after.get('timing_notice_lines')
    if not isinstance(old, list) or not isinstance(new, list): return []
    if len(new) != len(old)+1 or new[:-1] != old: return []
    try:
        match = TIMER_LINE.match(new[-1].encode('ascii'))
        duration = int(match.group(1)) if match else 0
    except (AttributeError, UnicodeError, ValueError): return []
    if not 0 < duration <= MAX_TIMER_NOTICE_NS: return []
    return [{'kind':'linux_hrtimer_notice', 'line':new[-1], 'duration_ns':duration,
             'policy':TIMING_POLICY, 'severity':'diagnostic', 'rt_guard_required':True}]


def audit_changed(before, after, key, mode, allow_timing_notice=False):
    if before[key] == after[key]:
        return False
    if key == 'dmesg_sha256' and allow_timing_notice and timing_notice_warnings(before, after, mode):
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
