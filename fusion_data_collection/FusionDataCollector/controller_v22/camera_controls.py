# -*- coding: utf-8 -*-
"""Camera-only UVC settings, compatible with controller Python 2.7.

Descriptor discovery reads sysfs. Brightness uses bounded synchronous USB
control transfers to one verified bus/address; no reset, driver, configuration,
kernel module, or robot interfaces are used. The caller must verify idle USB.
"""
from __future__ import print_function
import ctypes
import json
import os
import struct
import tempfile

PROFILES = {
    'mjpeg': dict(format='mjpeg', width=1024, height=768, fps=30),
    'mjpeg-1280x1024': dict(format='mjpeg', width=1280, height=1024, fps=30),
    'mjpeg-1920x1080': dict(format='mjpeg', width=1920, height=1080, fps=30),
    'yuyv': dict(format='yuyv', width=640, height=480, fps=30),
    'yuyv-320x240': dict(format='yuyv', width=320, height=240, fps=30),
}
MAX_DESCRIPTOR_BYTES = 1024 * 1024


def descriptors(data):
    data = bytearray(data)
    if not data or len(data) > MAX_DESCRIPTOR_BYTES:
        raise ValueError('Invalid USB descriptor size')
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError('Truncated USB descriptor')
        size = data[offset]
        if size < 2 or offset + size > len(data):
            raise ValueError('Invalid USB descriptor length')
        yield data[offset:offset + size]
        offset += size


def parse_capabilities(data):
    interface = None
    format_name = None
    modes = []
    brightness = None
    for block in descriptors(data):
        if block[1] == 4:
            if len(block) < 9:
                raise ValueError('Invalid USB interface descriptor')
            interface = (block[2], block[3], block[5], block[6])
            format_name = None
        elif block[1] == 0x24 and len(block) >= 3 and interface:
            subtype = block[2]
            if interface[1:] == (0, 14, 1) and subtype == 5:
                if len(block) < 10 or len(block) < 9 + block[7]:
                    raise ValueError('Invalid UVC processing unit')
                if block[7] and block[8] & 1:
                    candidate = (interface[0], block[3])
                    if brightness is not None and candidate != brightness:
                        raise ValueError('Ambiguous brightness processing unit')
                    brightness = candidate
            if interface[1:] != (0, 14, 2):
                continue
            if subtype == 6:
                format_name = 'mjpeg'
            elif subtype == 4:
                format_name = 'yuyv' if len(block) >= 27 and bytes(block[5:9]) == b'YUY2' else None
            elif subtype in (5, 7) and format_name:
                if len(block) < 26:
                    raise ValueError('Invalid UVC frame descriptor')
                width, height = struct.unpack_from('<HH', block, 5)
                count = block[25]
                if count:
                    if len(block) < 26 + count * 4:
                        raise ValueError('Truncated frame intervals')
                    rates = [struct.unpack_from('<I', block, 26 + i * 4)[0] for i in range(count)]
                    supported = 333333 in rates
                else:
                    if len(block) < 38:
                        raise ValueError('Truncated continuous frame intervals')
                    low, high, step = struct.unpack_from('<III', block, 26)
                    supported = low <= 333333 <= high and (not step or (333333 - low) % step == 0)
                if supported:
                    for key, profile in PROFILES.items():
                        if profile['format'] == format_name and (width, height) == (profile['width'], profile['height']):
                            modes.append(key)
    return dict(modes=sorted(set(modes), key=lambda key: (PROFILES[key]['width'] * PROFILES[key]['height'], key)),
                brightness=brightness)


def camera_capabilities(camera):
    if camera['usb_path'] not in ('1-1', '1-2', '1-4'):
        raise ValueError('Unsupported camera port')
    base = '/sys/bus/usb/devices/' + camera['usb_path']
    for name, expected in (('idVendor', camera['vid']), ('idProduct', camera['pid'])):
        with open(base + '/' + name) as handle:
            if handle.read().strip() != expected:
                raise RuntimeError('Camera identity changed')
    with open(base + '/descriptors', 'rb') as handle:
        capabilities = parse_capabilities(handle.read(MAX_DESCRIPTOR_BYTES + 1))
    # Raw YUYV at 640x480/30 alone needs 18.4 MB/s, exceeding the
    # deployed camera transport. Do not offer modes that overload recording.
    # Legacy raw decoding and native validation remain available to old data.
    capabilities['modes'] = [key for key in capabilities['modes'] if PROFILES[key]['format'] == 'mjpeg']
    return capabilities


def brightness(camera, info, unit, value=None):
    if unit is None:
        return dict(supported=False, message=u'相机未声明硬件亮度控制')
    usb = ctypes.CDLL('libusb-1.0.so.0')
    ptr = ctypes.c_void_p
    usb.libusb_init.argtypes = [ctypes.POINTER(ptr)]
    usb.libusb_exit.argtypes = [ptr]
    usb.libusb_get_device_list.argtypes = [ptr, ctypes.POINTER(ctypes.POINTER(ptr))]
    usb.libusb_get_device_list.restype = ctypes.c_ssize_t
    usb.libusb_free_device_list.argtypes = [ctypes.POINTER(ptr), ctypes.c_int]
    for name in ('libusb_get_bus_number', 'libusb_get_device_address'):
        getattr(usb, name).argtypes = [ptr]
        getattr(usb, name).restype = ctypes.c_uint8
    usb.libusb_open.argtypes = [ptr, ctypes.POINTER(ptr)]
    usb.libusb_close.argtypes = [ptr]
    usb.libusb_claim_interface.argtypes = [ptr, ctypes.c_int]
    usb.libusb_release_interface.argtypes = [ptr, ctypes.c_int]
    usb.libusb_control_transfer.argtypes = [ptr, ctypes.c_uint8, ctypes.c_uint8,
        ctypes.c_uint16, ctypes.c_uint16, ptr, ctypes.c_uint16, ctypes.c_uint32]
    context, handle = ptr(), ptr()
    devices = ctypes.POINTER(ptr)()
    claimed = None
    if usb.libusb_init(ctypes.byref(context)) < 0:
        raise RuntimeError('Cannot initialize camera USB control')
    try:
        count = usb.libusb_get_device_list(context, ctypes.byref(devices))
        if count < 0:
            raise RuntimeError('Cannot enumerate camera USB controls')
        for index in range(count):
            device = devices[index]
            if usb.libusb_get_bus_number(device) == info['bus'] and usb.libusb_get_device_address(device) == info['address']:
                if usb.libusb_open(device, ctypes.byref(handle)) < 0:
                    raise RuntimeError('Cannot open camera USB controls')
                break
        if not handle.value:
            raise RuntimeError('Camera USB address changed')
        interface, processing = unit
        if usb.libusb_claim_interface(handle, interface) != 0:
            raise RuntimeError('Cannot claim idle camera control interface')
        claimed = interface
        def transfer(request, setting=None):
            buffer = (ctypes.c_ubyte * 2)()
            if setting is not None:
                raw = struct.pack('<h', setting)
                buffer[0], buffer[1] = bytearray(raw)
            result = usb.libusb_control_transfer(handle, 0xa1 if setting is None else 0x21,
                request, 0x0200, processing * 256 + interface, buffer, 2, 750)
            if result != 2:
                raise RuntimeError('Hardware brightness USB request failed: %d' % result)
            return struct.unpack('<h', bytearray(buffer))[0]
        values = dict(supported=True, value=transfer(0x81), min=transfer(0x82),
                      max=transfer(0x83), step=transfer(0x84), default=transfer(0x87))
        if values['step'] <= 0 or not values['min'] <= values['value'] <= values['max'] or not values['min'] <= values['default'] <= values['max']:
            raise RuntimeError('Invalid hardware brightness range')
        if value is not None:
            if type(value) is not int or not values['min'] <= value <= values['max'] or (value - values['min']) % values['step']:
                raise ValueError('Brightness outside hardware range or step')
            transfer(0x01, value)
            values['value'] = transfer(0x81)
            if values['value'] != value:
                raise RuntimeError('Hardware brightness readback differs from requested value')
        return values
    finally:
        if handle.value:
            if claimed is not None:
                usb.libusb_release_interface(handle, claimed)
            usb.libusb_close(handle)
        if devices:
            usb.libusb_free_device_list(devices, 1)
        usb.libusb_exit(context)


def settings_path(service):
    return os.path.join(os.path.dirname(service.config['helper']), 'camera-settings.json')


def load_settings(service):
    path = settings_path(service)
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        result = json.load(handle)
    if not isinstance(result, dict):
        raise ValueError('Invalid camera settings file')
    return result


def save_settings(service, settings):
    path = settings_path(service)
    fd, temporary = tempfile.mkstemp(prefix='.camera-settings-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(settings, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_for(service, camera_id):
    selected = load_settings(service).get(camera_id, {}).get('mode_id', 'mjpeg')
    if selected not in PROFILES:
        raise ValueError('Unsupported saved camera mode')
    return selected, dict(PROFILES[selected])


def control(service, request, api):
    """Idle-only authenticated camera request; caller already verified the token."""
    if api.ACTIVE is not None or service.fault:
        raise RuntimeError('Stop camera preview and resolve camera faults before settings')
    camera = next((c for c in service.config['cameras'] if c['id'] == request.get('camera_id')), None)
    if camera is None:
        raise ValueError('Unknown camera')
    info = api.device_info(camera)
    if not info['available'] or not api.interface_idle(camera):
        raise RuntimeError('Camera missing or interface busy')
    import subprocess
    with open(os.devnull, 'wb') as null:
        code = subprocess.call(['/bin/fuser', '/dev/bus/usb/%03d/%03d' % (info['bus'], info['address'])], stdout=null, stderr=null)
    if code != 1:
        raise RuntimeError('Camera ownership check failed')
    capabilities = camera_capabilities(camera)
    settings = load_settings(service)
    saved = dict(settings.get(camera['id'], {}))
    mode_id = request.get('mode_id', saved.get('mode_id', 'mjpeg'))
    if mode_id not in capabilities['modes']:
        raise ValueError('Camera does not advertise this mode at 30 fps')
    value = request.get('brightness')
    previous_brightness = brightness(camera, info, capabilities['brightness'])
    rollback = dict(mode_id=saved.get('mode_id', 'mjpeg'))
    if previous_brightness.get('supported'):
        rollback['brightness'] = previous_brightness['value']
    if 'mode_id' in request or value is not None:
        service.camera_settings_rollback[camera['id']] = rollback
    actual = brightness(camera, info, capabilities['brightness'], value) if value is not None else previous_brightness
    if value is not None and not actual['supported']:
        raise ValueError('Camera does not support hardware brightness')
    if 'mode_id' in request or value is not None:
        saved['mode_id'] = mode_id
        if value is not None:
            saved['brightness'] = actual['value']
        settings[camera['id']] = saved
        save_settings(service, settings)
    options = [dict(PROFILES[key], id=key, label=u'%d × %d · 30 帧/秒 · %s' % (
        PROFILES[key]['width'], PROFILES[key]['height'], PROFILES[key]['format'].upper())) for key in capabilities['modes']]
    return dict(ok=True, camera_id=camera['id'], brightness=actual,
                resolution=dict(options=options, selected=next(p for p in options if p['id'] == mode_id), editable=True))


def restore(service, request, api):
    """Restore the service's own pre-write snapshot, without hiding USB faults.

    Restoring a disk preference is safe even after capture fails. Hardware is
    reapplied and read back by the next successful stream, never claimed here.
    """
    if api.ACTIVE is not None:
        raise RuntimeError('Stop camera stream before restoring configuration')
    key = request.get('camera_id')
    previous = service.camera_settings_rollback.get(key)
    if previous is None:
        raise ValueError('No previous verified camera configuration')
    settings = load_settings(service)
    settings[key] = dict(previous)
    save_settings(service, settings)
    del service.camera_settings_rollback[key]
    return dict(ok=True, camera_id=key, restored_configuration=True, hardware_readback_deferred=True)
