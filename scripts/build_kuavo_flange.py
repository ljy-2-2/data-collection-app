#!/usr/bin/env python3
"""Extract the supplied flange's saved display mesh locally; no CAD or robot IO.

Format references (independent public format research, not SolidWorks APIs):
https://github.com/blussyya/sldprt-format-research/blob/main/docs/format/container.md
https://github.com/blussyya/sldprt-format-research/blob/main/docs/format/displaylists.md
This bounded reader is for the bundled DL_VERSION_18000 part, not a general CAD
converter. It retains the CAD coordinate system and does not infer assembly poses.
"""
import hashlib
import json
from pathlib import Path
import struct
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'teleop_control/assets/kuavo/flange'


def streams(data):
    assert data[4:8] == b'\0\0\0\4', 'Unsupported SolidWorks container'
    result = {}
    cursor = 0
    while (cursor := data.find(b'\x14\x00\x06\x00\x08\x00', cursor)) >= 0:
        offset = cursor
        cursor += 6
        if offset + 26 > len(data):continue
        crc, packed, size, name_size = struct.unpack_from('<4I', data, offset + 10)
        if crc < 65536 or not 0 < name_size < 512 or not 0 < packed <= len(data) or size > 16_000_000:continue
        start = offset + 26 + name_size
        if start + packed > len(data):continue
        name_bytes = bytes((v >> 4) | ((v & 15) << 4) for v in data[offset+26:start])
        if not all(32 <= v < 127 for v in name_bytes):continue
        name = name_bytes.decode('ascii')
        if name in result:continue
        payload = zlib.decompress(data[start:start+packed], -15)
        assert len(payload) == size and zlib.crc32(payload) == crc, name
        result[name] = payload
    assert '_DL_VERSION_18000/DLUpdateStamp' in result, 'Unvalidated display-list version'
    return result


def display_mesh(data):
    def array(offset, stride, kind, dtype):
        s, k, marker, count = struct.unpack_from('<4I', data, offset)
        assert (s, k, marker) == (stride, kind, 2) and count < 100_000
        end = offset + 16 + stride * count
        assert end <= len(data)
        values = np.frombuffer(data[offset+16:end], dtype=dtype).copy()
        if stride > np.dtype(dtype).itemsize:values = values.reshape(count, stride // np.dtype(dtype).itemsize)
        return values, end

    positions, triangles, boxes = [], [], []
    cursor = 0
    while (cursor := data.find(struct.pack('<3I', 4, 8, 2), cursor)) >= 0:
        start = cursor
        cursor += 1
        count = struct.unpack_from('<I', data, start+12)[0]
        next_array = start + 16 + count * 4
        if next_array + 12 > len(data) or data[next_array:next_array+12] != struct.pack('<3I', 12, 100, 2):continue
        # Once a strip/position pair is identified, reject malformed faces instead
        # of silently returning a partial part.
        lengths, offset = array(start, 4, 8, '<u4')
        vertices, offset = array(offset, 12, 100, '<f4')
        normals, offset = array(offset, 12, 100, '<f4')
        edges, offset = array(offset, 4, 8, '<u4')
        sections, offset = array(offset, 4, 8, '<u4')
        flags, offset = array(offset, 1, 8, 'u1')
        assert len(lengths) and min(lengths) >= 3
        assert len(vertices) == sum(lengths) == len(normals)
        assert np.array_equal(sections, 2*lengths-2)
        assert len(edges) == len(flags) == sum(sections)
        assert np.isfinite(vertices).all() and np.max(abs(np.linalg.norm(normals, axis=1)-1)) < .001
        assert struct.unpack_from('<3I', data, offset) == (0, 0, 1)
        center = np.array(struct.unpack_from('<3d', data, offset+12))
        maximum = np.array(struct.unpack_from('<3d', data, offset+36))
        minimum = np.array(struct.unpack_from('<3d', data, offset+60))
        assert np.allclose(center, (minimum+maximum)/2, atol=1e-12)
        assert np.all(vertices >= minimum-1e-7) and np.all(vertices <= maximum+1e-7)
        boxes.append([minimum, maximum])
        base = len(positions)
        positions.extend(vertices.tolist())
        for length in lengths:
            for i in range(2, int(length)):
                triangles.append([base+i-2, base+i-1, base+i] if i%2 == 0 else [base+i-1, base+i-2, base+i])
            base += int(length)
    assert len(boxes) == 21, 'Supplied flange should have 21 saved faces'
    bounds = [np.min(np.array(boxes)[:,0], axis=0), np.max(np.array(boxes)[:,1], axis=0)]
    return dict(vertices=positions, faces=triangles), np.array(bounds)


def build():
    source = ASSETS / 'motor_adapter.SLDPRT'
    raw = source.read_bytes()
    content = streams(raw)
    mesh, bounds = display_mesh(content['Contents/DisplayLists'])
    target = ASSETS / 'display-mesh.json'
    target.write_text(json.dumps(mesh, separators=(',', ':'))+'\n')
    (ASSETS / 'preview.png').write_bytes(content['PreviewPNG'])
    manifest = dict(
        original_filename='乐聚电机连接法兰.SLDPRT',
        source_sha256=hashlib.sha256(raw).hexdigest(),
        display_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        units='m', bounds_m=bounds.tolist(), dimensions_mm=((bounds[1]-bounds[0])*1000).round(6).tolist(),
        vertices=len(mesh['vertices']), triangles=len(mesh['faces']), saved_faces=21,
        assembly=dict(status='provisional_holes_aligned',
                      confirmed_by_user='Small diameter end to robot end effector; large diameter end to gripper. Tianji adapter is not used on Kuavo.',
                      robot_side_diameter_mm=48.02, gripper_side_diameter_mm=56.8,
                      large_section_length_mm=15.2, small_section_length_mm=15,
                      robot_side_face_center_cad_mm=[0, 0, 22.5],
                      gripper_side_face_center_cad_mm=[0, 0, 52.7],
                      tianji_adapter_reuse=False,
                      excluded_tianji_adapter_meshes=dict(left='m54', right='m116'),
                      mount_configuration='../gripper_mounts.json'),
        note='Saved CAD display tessellation only. No control URDF/TCP changes. Total length is not an inferred assembled offset.')
    (ASSETS / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    build()
