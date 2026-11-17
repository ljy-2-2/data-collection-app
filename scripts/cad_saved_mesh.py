"""Read saved SolidWorks block streams and tessellation, without a CAD kernel.

Container/descriptor grammar: cadmpeg, Apache-2.0 implementation and
CC-BY-4.0 format documentation: https://github.com/cadmpeg/cadmpeg
No feature rebuild or mate solving is performed.
"""
from pathlib import Path
import struct
import zlib
import numpy as np
import trimesh


def streams(path):
    data = Path(path).read_bytes()
    marker = bytes.fromhex('140006000800')
    pos = 0
    result = {}
    while (pos := data.find(marker, pos)) >= 0:
        start = pos
        pos += 1
        try:
            _, crc, compressed, size, n = struct.unpack_from('<5I', data, start + 6)
            if n > 4096 or compressed > len(data) or size > 500_000_000:
                continue
            raw_name = data[start + 26:start + 26 + n]
            name = bytes(((x << 4) & 255) | (x >> 4) for x in raw_name).decode('utf-8')
            raw = zlib.decompress(data[start + 26 + n:start + 26 + n + compressed], -15)
            if len(raw) == size and zlib.crc32(raw) == crc:
                result[name] = raw
        except (ValueError, zlib.error, struct.error):
            continue
    if not result:
        raise ValueError(f'No validated SolidWorks block streams: {path}')
    return result


def tables(data):
    """Validate all six channels before accepting a face table (metres)."""
    pos = 0
    marker = struct.pack('<3I', 4, 8, 2)
    while (pos := data.find(marker, pos)) >= 0:
        start = pos
        pos += 1
        try:
            channels = []
            at = start
            for item, kind in [(4,8), (12,100), (12,100), (4,8), (4,8), (1,8)]:
                it, ki, flag, count = struct.unpack_from('<4I', data, at)
                if (it,ki,flag) != (item,kind,2) or at+16+it*count > len(data):
                    raise ValueError('Invalid descriptor')
                channels.append((count, data[at+16:at+16+it*count]))
                at += 16+it*count
            strips = np.frombuffer(channels[0][1], '<u4')
            c = int(strips.sum())
            if not len(strips) or np.any(strips < 3) or c != channels[1][0] or channels[2][0] not in [0,c]:
                continue
            lc = np.frombuffer(channels[4][1], '<u4')
            if not np.array_equal(lc, 2*strips-2):
                continue
            e = int(lc.sum())
            if (channels[3][0], channels[5][0]) not in [(0,0),(e,e)]:
                continue
            v = np.frombuffer(channels[1][1], '<f4').reshape(-1,3).astype(float)
            if not np.isfinite(v).all():
                continue
            fs = []
            base = 0
            for length in strips:
                n = int(length)
                ids = np.arange(n-2)+base
                f = np.column_stack((ids, ids+1, ids+2))
                f[1::2,1:] = f[1::2,1:][:,::-1]
                fs.append(f)
                base += n
            f = np.concatenate(fs)
            if channels[2][0]:
                normals = np.frombuffer(channels[2][1],'<f4').reshape(-1,3)
                geometric = np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
                flip = np.einsum('ij,ij->i', geometric, normals[f].mean(axis=1)) < 0
                f[flip] = f[flip][:,[0,2,1]]
            yield start, at, trimesh.Trimesh(v, f, process=False)
            pos = at
        except (ValueError, struct.error):
            pass


def part_mesh(path):
    parts = streams(path)
    data = parts.get('Contents/DisplayLists', b'')
    faces = [m for _,_,m in tables(data)]
    if not faces:
        raise ValueError(f'No inline tessellation in {path}')
    mesh = trimesh.util.concatenate(faces)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    return mesh


