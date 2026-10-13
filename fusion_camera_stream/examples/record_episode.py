#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fusion_camera import FusionStreamClient
from fusion_camera.episode import record_episode

p=argparse.ArgumentParser(description='Three cameras and both arms over TLS; optional experimental read-only gripper queries. No motion commands.')
p.add_argument('--host',default='192.168.1.190')
p.add_argument('--seconds',type=int,default=60)
p.add_argument('--hz',type=int,choices=[30],default=30)
p.add_argument('--joint-hz',type=int,choices=[30,100],default=100)
p.add_argument('--output',type=Path,default=Path.home()/'桌面'/'1')
p.add_argument('--credentials',type=Path)
p.add_argument('--grippers',action='store_true',help='EXPERIMENTAL: 60-second test not passed. Exclusive stationary read-only SDK session; never alongside an App control owner')
p.add_argument('--gripper-hz',type=int,choices=[10,20,30],default=20)
a=p.parse_args()
with ExitStack() as stack:
    client=stack.enter_context(FusionStreamClient(a.host,credentials=a.credentials))
    grippers=None
    if a.grippers:
        from fusion_camera.grippers import ReadOnlyGrippers
        grippers=stack.enter_context(ReadOnlyGrippers(a.host))
    path,validation=record_episode(client,a.output,seconds=a.seconds,hz=a.hz,joint_hz=a.joint_hz,
        gripper_source=grippers,gripper_hz=a.gripper_hz,progress=lambda s:print(s,flush=True))
print(path,flush=True)
print(validation,flush=True)
sys.exit(0 if validation['acquisition_passed'] else 1)
