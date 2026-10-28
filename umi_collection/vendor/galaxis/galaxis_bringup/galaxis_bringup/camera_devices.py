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
            # usbN / N-port numbering can change at boot. The controller and
            # downstream port chain identify the same socket across boots.
            root = next((p for p in usb.parents if p.name.startswith('usb')
                         and p.name[3:].isdigit()), None)
            if root is not None and '-' in usb.name:
                controller = root.parent
                if controller.name.startswith('xhci-hcd.') and controller.name.endswith('.auto'):
                    controller = controller.with_name('xhci-hcd')
                identity['port_path'] = str(controller) + ':' + usb.name.split('-', 1)[1]
            stat = device.stat()
            found.append(dict(device=str(device), name=name, **identity,
                              hub=usb.name.rsplit('.', 1)[0] if '.' in usb.name else usb.name,
                              generation=f'{entry.resolve()}:{stat.st_ino}:{stat.st_rdev}'))
            if identity['vendor'] == '2b03' and identity['product'] in ('f680','f682'):
                found[-1]['name'] = 'ZED Mini · 中心相机（左目）'
                found[-1]['backend'] = 'zed-uvc'
        except (OSError, StopIteration):
            continue
    for item in found:
        same = [x for x in found if all(x[k] == item[k] for k in ('vendor', 'product', 'serial'))]
        item['stable_id'] = ('serial:'+':'.join(item[k] for k in ('vendor','product','serial'))
                             if item['serial'] and len(same) == 1 else 'usb:'+item.get('port_path', item['usb_path']))
    return sorted(found, key=lambda d: (d['usb_path'], d['device']))


def identity(item):
    return {k: item.get(k, '') for k in ('vendor', 'product', 'serial', 'usb_path', 'port_path')}


def _compatible(saved, candidate):
    return (all(candidate.get(k) == saved.get(k) for k in ('vendor', 'product'))
            and (not saved.get('serial') or candidate.get('serial') == saved['serial']))


def _same_port(saved, candidate):
    if saved.get('port_path'):
        return saved['port_path'] == candidate.get('port_path')
    return bool(saved.get('usb_path')) and saved['usb_path'] == candidate.get('usb_path')


def match(saved, candidates):
    """Never substitute a different camera because Linux recycled its video number."""
    if not saved:
        return None
    matches = [c for c in candidates if _compatible(saved, c)]
    at_port = [c for c in matches if _same_port(saved, c)]
    if len(at_port) == 1:
        return at_port[0]
    if saved.get('serial'):
        matches = [c for c in matches if c.get('serial') == saved['serial']]
        if len(matches) == 1:
            return matches[0]
    return None


def read_selection(path):
    try:
        result = json.loads(Path(path).read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_roles(saved, candidates):
    matches = {}
    for role, value in saved.items():
        compatible = [c for c in candidates if _compatible(value, c)]
        at_port = [c for c in compatible if _same_port(value, c)]
        item = at_port[0] if len(at_port) == 1 else None
        # A generic serial shared by two saved roles is never a unique ID,
        # even while one camera is unplugged. Do not steal the remaining one.
        serial_unique = value.get('serial') and sum(
            all(other.get(k) == value.get(k) for k in ('vendor', 'product', 'serial'))
            for other in saved.values()) == 1
        if item is None and serial_unique and len(compatible) == 1:
            item = compatible[0]
        matches[role] = item
    # Two indistinguishable cameras must be explicitly assigned, never duplicated.
    return {role: item for role, item in matches.items() if item and
            sum(other is not None and other['device'] == item['device'] for other in matches.values()) == 1}
