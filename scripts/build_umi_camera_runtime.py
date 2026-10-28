#!/usr/bin/env python3
"""Build the portable camera library inside the installed native UMI image."""
import argparse, hashlib, json, os, platform, subprocess
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/third_party/usb_cam')
    args=parser.parse_args()
    arch={'x86_64':'amd64','aarch64':'arm64'}[platform.machine()]
    source=args.source.resolve();out=source/'native'/arch;out.mkdir(parents=True,exist_ok=True)
    build='''set -eu
pkg-config --exists opencv4 libavcodec libavutil libswscale
g++ -std=c++17 -O2 -fPIC -shared -Wl,-soname,libusb_cam.so -I/source/include /source/src/usb_cam.cpp -o /out/libusb_cam.so $(pkg-config --cflags --libs opencv4 libavcodec libavutil libswscale)
cat > /out/check.cpp <<'CPP'
#include "usb_cam/usb_cam.hpp"
int main() {
 if (usb_cam::utils::epoch_time_shift_us(timeval{12,987654}, timespec{3,123456789}) != 9864197) return 3;
 auto stamp = usb_cam::utils::calc_img_timestamp(timeval{3,123457}, 9864197);
 if (stamp.tv_sec != 12 || stamp.tv_nsec != 987654000) return 4;
 usb_cam::UsbCam camera;
 if (camera.number_of_buffers() != 8) return 1;
 timeval epoch; timespec mono;
 gettimeofday(&epoch, nullptr); clock_gettime(CLOCK_MONOTONIC, &mono);
 auto expected = epoch.tv_sec * 1000000LL + epoch.tv_usec - mono.tv_sec * 1000000LL - mono.tv_nsec / 1000;
 auto actual = usb_cam::utils::get_epoch_time_shift_us();
 return std::abs(actual - expected) < 50000 ? 0 : 2;
}
CPP
g++ -std=c++17 -I/source/include /out/check.cpp -L/out -lusb_cam -o /out/check $(pkg-config --cflags --libs opencv4 libavcodec libavutil libswscale)
LD_LIBRARY_PATH=/out /out/check
rm /out/check /out/check.cpp
'''
    subprocess.run(['docker','run','--rm','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
        '-v',f'{source}:/source:ro','-v',f'{out}:/out','--entrypoint','/bin/bash',args.image,'-c',build],check=True)
    lib=out/'libusb_cam.so'
    report=dict(architecture=arch,runtime_image=args.image,sha256=hashlib.sha256(lib.read_bytes()).hexdigest(),
                source_sha256=hashlib.sha256((source/'src/usb_cam.cpp').read_bytes()).hexdigest(),capture_buffers=8,
                headers_sha256={str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((source/'include').rglob('*.hpp'))})
    (out/'build.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':main()
