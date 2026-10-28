"""ZED USB stereo video on both ARM and x86, using standard UVC/V4L2.

The existing center-camera contract is one image: use the left optical sensor
at its native size. Depth and tracking continue to be separate from UMI poses.
"""
from __future__ import annotations

import configparser
import math
from pathlib import Path
import re
import urllib.request

VIDEO_PRODUCTS = {'f680', 'f682'}  # ZED Mini UVC; f681 is HID only.
SIZES = {(672, 376): 'VGA', (1280, 720): 'HD', (1920, 1080): 'FHD'}


def connection_issues(sys_usb=Path('/sys/bus/usb/devices')):
    """An IMU-only enumeration must never count as a working camera."""
    products = []
    try:
        for p in sys_usb.iterdir():
            try:
                if (p/'idVendor').read_text().strip().lower() == '2b03':
                    products.append((p/'idProduct').read_text().strip().lower())
            except OSError:
                continue
    except OSError:
        return []
    if 'f681' in products and not VIDEO_PRODUCTS.intersection(products):
        return ['ZED Mini 仅识别到传感器接口，视频接口未连接。请检查 USB 3.0 数据线和接口；当前无法预览或录制中心相机。']
    return []


def is_zed(item):
    return item.get('vendor', '').lower() == '2b03' and item.get('product', '').lower() in VIDEO_PRODUCTS


def is_zed_device(device, sys_class=Path('/sys/class/video4linux')):
    try:
        entry = (sys_class/Path(device).resolve().name/'device').resolve(strict=True)
        for p in (entry, *entry.parents):
            if (p/'idVendor').exists():
                return is_zed({'vendor':(p/'idVendor').read_text().strip(),
                               'product':(p/'idProduct').read_text().strip()})
    except OSError:
        pass
    return False


def mono_modes(modes):
    """V4L2 advertises the side-by-side size; expose a single eye to UMI."""
    result = []
    for m in modes:
        width, height = m['width']//2, m['height']
        if m['fourcc'] != 'YUYV' or m['width'] % 2 or (width, height) not in SIZES:
            continue
        result.append({**m, 'width':width, 'backend':'zed-uvc',
            'id':f'zed-left-{width}x{height}-30',
            'label':f'ZED Mini 左目 · {width}×{height} · 30帧'})
    return result


def calibration(text, width, height):
    config = configparser.ConfigParser()
    config.read_string(text)
    section = config['LEFT_CAM_'+SIZES[(width,height)]]
    values = {k:float(section[k]) for k in ('fx','fy','cx','cy','k1','k2','p1','p2','k3')}
    if not all(math.isfinite(v) for v in values.values()) or min(values['fx'],values['fy']) <= 0:
        raise ValueError('ZED factory calibration is invalid')
    return values


def serial_number(value):
    value = str(value).removeprefix('SN')
    if not re.fullmatch(r'[1-9][0-9]{0,11}', value):
        raise ValueError('ZED serial number is invalid')
    return value


def left_image_layout(width, height, encoding, step):
    channels = {'bgr8':3,'rgb8':3,'mono8':1}.get(encoding)
    if not channels or width % 2 or (width//2,height) not in SIZES or step < width*channels:
        raise ValueError('Invalid ZED stereo image layout')
    return width//2, width//2*channels


def ensure_calibration(serial, directory):
    serial = serial_number(serial)
    path = Path(directory)/f'SN{serial}.conf'
    if path.is_file():
        text = path.read_text()
    else:
        try:
            with urllib.request.urlopen('https://calib.stereolabs.com/?SN='+serial, timeout=15) as response:
                text = response.read(65537).decode('utf-8')
        except (OSError, UnicodeError) as exc:
            raise RuntimeError('无法获取 ZED 出厂标定，请联网后重新配置中心相机') from exc
        if len(text) > 65536:
            raise ValueError('ZED calibration response is too large')
        for size in SIZES:
            calibration(text, *size)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(text)
        temporary.replace(path)
    for size in SIZES:
        calibration(text, *size)
    return path

