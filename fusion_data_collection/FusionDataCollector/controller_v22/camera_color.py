"""Read UVC YUY2 color-matching descriptors without opening a camera stream.

Compatible with controller Python 2.7 and laptop Python 3. Matrix identifiers
1 and 4 are the profiles verified on the installed cameras. UVC color-matching
descriptors do not specify quantization range: limited range is an explicitly
reported assumption, not a value claimed to have been read from hardware.
"""
from __future__ import print_function

import os

YUY2_GUID = bytearray(b'YUY2\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71')
MATRICES = {1: 'bt709', 4: 'bt601'}
MAX_DESCRIPTOR_BYTES = 1024*1024
# UVC VideoStreaming format descriptor subtypes. Frame descriptors and the
# color descriptor are subordinate to the most recent format descriptor.
FORMAT_SUBTYPES = frozenset((0x04, 0x06, 0x0a, 0x0c, 0x10, 0x12, 0x13, 0x15, 0x16, 0x18))


def parse_camera_color(descriptors):
    """Return the unambiguous color description of alt-0 YUY2 formats.

    Unrelated MJPEG/color/interface descriptors never supply YUY2 metadata.
    Multiple YUY2 formats must each declare the same color profile; otherwise
    a caller would need to identify a specific selected interface/format.
    """
    if not isinstance(descriptors, (bytes, bytearray)):
        raise ValueError('USB descriptors must be bytes')
    data = bytearray(descriptors)
    if not data:
        raise ValueError('USB descriptors are empty')
    if len(data) > MAX_DESCRIPTOR_BYTES:
        raise ValueError('USB descriptor data exceeds size limit')
    offset = 0
    configuration_end = None
    streaming = False
    selected = None
    formats = []
    while offset < len(data):
        if configuration_end == offset:
            configuration_end = None
            streaming, selected = False, None
        if offset+2 > len(data):
            raise ValueError('Truncated USB descriptor header at %d' % offset)
        size, kind = data[offset], data[offset+1]
        if size < 2 or offset+size > len(data):
            raise ValueError('Invalid USB descriptor length at %d' % offset)
        if configuration_end is not None and offset+size > configuration_end:
            raise ValueError('USB descriptor crosses configuration boundary')
        block = data[offset:offset+size]
        if kind == 0x02:  # A new configuration cannot inherit an interface.
            if size < 9:
                raise ValueError('Truncated USB configuration descriptor')
            total = block[2]+256*block[3]
            if configuration_end is not None or total < size or offset+total > len(data):
                raise ValueError('Invalid USB configuration total length')
            configuration_end = offset+total
            streaming, selected = False, None
        elif kind == 0x04:
            if size < 9:
                raise ValueError('Truncated USB interface descriptor')
            streaming = block[3] == 0 and block[5] == 0x0e and block[6] == 0x02
            selected = None
        elif kind == 0x24:
            if size < 3:
                raise ValueError('Truncated UVC class-specific descriptor')
            subtype = block[2]
            if streaming and subtype in FORMAT_SUBTYPES:
                selected = None
                if subtype == 0x04:  # VS_FORMAT_UNCOMPRESSED
                    if size < 27:
                        raise ValueError('Truncated UVC uncompressed format descriptor')
                    if block[5:21] == YUY2_GUID:
                        if block[3] == 0 or block[21] != 16:
                            raise ValueError('Invalid UVC YUY2 format index or pixel depth')
                        selected = {'color': None}
                        formats.append(selected)
            elif streaming and subtype == 0x0d and selected is not None:
                if size != 6:
                    raise ValueError('Invalid UVC color-matching descriptor length')
                if selected['color'] is not None:
                    raise ValueError('Duplicate UVC color-matching descriptor for YUY2')
                primaries, transfer, matrix = block[3], block[4], block[5]
                if matrix not in MATRICES:
                    raise ValueError('Unsupported UVC YUY2 color matrix %d' % matrix)
                selected['color'] = {
                    'yuv_matrix': MATRICES[matrix],
                    'uvc_color_matching': {'primaries': primaries, 'transfer': transfer, 'matrix': matrix},
                    'yuv_range': 'limited',
                    'limited_range_assumption': True,
                }
            elif streaming and subtype not in (1, 2, 3, 5, 7, 13, 17, 20, 23):
                # Do not attach an unknown format's color descriptor to YUY2.
                selected = None
        offset += size
    if not formats:
        raise ValueError('No YUY2 format on a UVC VideoStreaming alt-0 interface')
    colors = [item['color'] for item in formats]
    if any(color is None for color in colors):
        raise ValueError('Missing UVC color-matching descriptor for YUY2')
    if any(color != colors[0] for color in colors[1:]):
        raise ValueError('Conflicting UVC YUY2 color-matching descriptors')
    return colors[0]


def read_camera_color(usb_path, sysfs_root='/sys/bus/usb/devices'):
    """Read only this installation's fixed physical camera ports."""
    if usb_path not in ('1-1', '1-2', '1-4'):
        raise ValueError('Unsupported camera USB path: %s' % usb_path)
    path = os.path.join(sysfs_root, usb_path, 'descriptors')
    with open(path, 'rb') as descriptor_file:
        return parse_camera_color(descriptor_file.read(MAX_DESCRIPTOR_BYTES+1))
