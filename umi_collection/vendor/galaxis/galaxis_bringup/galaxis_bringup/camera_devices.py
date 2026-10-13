"""USB camera discovery and unambiguous identity matching, without opening streams."""
import fcntl
import json
import os
from pathlib import Path
import struct


def capture_device(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            capability = bytearray(104)
            fcntl.ioctl(fd, 0x80685600, capability)  # VIDIOC_QUERYCAP
            caps, device_caps = struct.unpack_from('II', capability, 84)
            caps = device_caps if caps & 0x80000000 else caps
            return bool(caps & (0x1 | 0x1000))  # VIDEO_CAPTURE / VIDEO_CAPTURE_MPLANE
        finally:
            os.close(fd)
    except OSError:
        return False


def discover(sys_class=Path('/sys/class/video4linux'), dev_root=Path('/dev'), probe=capture_device):
    found = []
    for entry in sorted(sys_class.glob('video*')):
        device = dev_root / entry.name
        try:
            name = (entry / 'name').read_text().strip()
            if any(marker in name.lower() for marker in ('true vision', 'integrated camera', 'facetime')):
                continue
            # Metadata nodes are not cameras, even when video numbering changes.
            if not probe(str(device)):
                continue
            usb = next(p for p in (entry.resolve(), *entry.resolve().parents) if (p / 'idVendor').is_file())
            identity = dict(vendor=(usb/'idVendor').read_text().strip(),
                            product=(usb/'idProduct').read_text().strip(),
                            serial=(usb/'serial').read_text().strip() if (usb/'serial').exists() else '',
                            usb_path=usb.name)
            stat = device.stat()
            found.append(dict(device=str(device), name=name, **identity,
                              hub=usb.name.rsplit('.', 1)[0] if '.' in usb.name else usb.name,
                              generation=f'{entry.resolve()}:{stat.st_ino}:{stat.st_rdev}'))
        except (OSError, StopIteration):
            continue
    for item in found:
        same = [x for x in found if all(x[k] == item[k] for k in ('vendor', 'product', 'serial'))]
        item['stable_id'] = ('serial:'+':'.join(item[k] for k in ('vendor','product','serial'))
                             if item['serial'] and len(same) == 1 else 'usb:'+item['usb_path'])
    return sorted(found, key=lambda d: (d['usb_path'], d['device']))


def identity(item):
    return {k: item.get(k, '') for k in ('vendor', 'product', 'serial', 'usb_path')}


def match(saved, candidates):
    """Never substitute a different camera because Linux recycled its video number."""
    if not saved:
        return None
    matches = [c for c in candidates if all(c.get(k) == saved.get(k) for k in ('vendor','product'))]
    if saved.get('serial'):
        matches = [c for c in matches if c.get('serial') == saved['serial']]
        if len(matches) == 1:
            return matches[0]
    matches = [c for c in matches if c.get('usb_path') == saved.get('usb_path')]
    return matches[0] if len(matches) == 1 else None


def read_selection(path):
    try:
        result = json.loads(Path(path).read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_roles(saved, candidates):
    matches = {role: match(value, candidates) for role, value in saved.items()}
    # Two indistinguishable cameras must be explicitly assigned, never duplicated.
    return {role: item for role, item in matches.items() if item and
            sum(other is not None and other['device'] == item['device'] for other in matches.values()) == 1}
