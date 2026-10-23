#!/usr/bin/env python3
"""Build the RK3588 streaming recorder helper on the target ARM64 system."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, default=Path(__file__).resolve().parents[1]
                        / 'umi_collection/vendor/galaxis/galaxis-core/galaxis-core')
    args = parser.parse_args()
    if platform.machine() not in ('aarch64', 'arm64'):
        raise SystemExit('Build this helper on the ARM64 target; desktop uses FFmpeg.')
    source = args.core / 'native/mpp_jpeg_h264.c'
    root = args.core / 'galaxis_core/native'
    root.mkdir(parents=True, exist_ok=True)
    library = Path('/usr/lib/aarch64-linux-gnu/librockchip_mpp.so.1').resolve(strict=True)
    binary = root / 'mpp-jpeg-h264'
    subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-I/usr/include/rockchip', str(source),
                    '-lrockchip_mpp', '-ljpeg', '-Wl,-rpath,$ORIGIN', '-o', str(binary)], check=True)
    bundled_library = root / 'librockchip_mpp.so.1'
    shutil.copy2(library, bundled_library)
    notice = Path('/usr/share/doc/librockchip-mpp1/copyright')
    if not notice.is_file():
        raise RuntimeError('The installed MPP copyright notice is required for distribution.')
    shutil.copy2(notice, root / 'LICENSE.rockchip-mpp')
    manifest = {
        'architecture': 'aarch64',
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [binary, bundled_library, root / 'LICENSE.rockchip-mpp']},
    }
    (root / 'build.json').write_text(json.dumps(manifest, indent=2))
    print('Built', binary)


if __name__ == '__main__':
    main()
