"""Exclusive, stationary-test reader for the existing DM-J4310 grippers.

Only CAN 0x7ff / function0x33 / whitelisted RO registers. No enable, disable,
zeroing, mode/watchdog changes, cache clear, motion or flash writes.
Uses OnLinkTo, deliberately NOT Connect (Connect changes vendor log settings).
An App that already owns the robot connection must provide its existing
feedback; never create a second vendor SDK session alongside that owner.
"""
import ctypes
import fcntl
import hashlib
import json
import math
from pathlib import Path
import socket
import struct
import threading
import time

SDK = Path.home()/'桌面/TJ_FX_ROBOT_CONTRL_SDK-master'
REGISTERS={0x50:'position_rad',0x51:'output_encoder_rad',0x3D:'driver_temperature_c',0x3E:'motor_temperature_c'}

def query_bytes(motor_id, register):
    if motor_id!=1 or register not in REGISTERS:
        raise ValueError('Only commissioned ID1 and read-only telemetry registers are permitted')
    return struct.pack('<IHBB',0x7ff,motor_id,0x33,register)

class ReadOnlyGrippers:
    """One vendor connection, intended only when no robot-control owner is active."""
    query_only_reader=True
    def __init__(self,host='192.168.1.190',sdk_root=SDK):
        self.root=Path(sdk_root)
        self.host=host
        self.lib=None
        self.lock=None
        self.config=json.loads((self.root/'Marvin-VR-Teleop/gripper/config.json').read_text())
        if self.config['transport']!='marvin_can' or self.config['tx_id_bytes']!=4 or self.config['rx_id_bytes']!=4:
            raise ValueError('Unverified gripper transport layout')
        for m in self.config['motors'].values():
            if m['motor_id']!=1 or m['master_id']!=0: raise ValueError('Unexpected gripper IDs')
        self.trace=[]
        self.duplicate_replies=0
        self.unexpected_reply=None
        self.temperature_cache={'left':{},'right':{}}
        self.closed=False

    def connect(self):
        # Fail rather than sharing/stealing an SDK socket. Native bind rechecks
        # after this advisory check, so an intervening owner also makes it fail.
        check=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try: check.bind(('0.0.0.0',4730))
        finally: check.close()
        self.lock=open('/tmp/fusion-gripper-reader.lock','a')
        fcntl.flock(self.lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.lib=ctypes.CDLL(str(self.root/'SDK_PYTHON/libMarvinSDK.so'))
        self.lib.OnLinkTo.argtypes=[ctypes.c_ubyte]*4; self.lib.OnLinkTo.restype=ctypes.c_bool
        self.lib.OnRelease.argtypes=[];self.lib.OnRelease.restype=ctypes.c_bool
        self.lib.OnGetBuf.argtypes=[ctypes.c_void_p];self.lib.OnGetBuf.restype=ctypes.c_bool
        self.lib.OnLocalLogOff.argtypes=[];self.lib.OnLocalLogOff.restype=None
        self.lib.OnLocalLogOff()  # Local verbosity only; never OnLogOff/LogSwitch.
        ptr=ctypes.POINTER(ctypes.c_ubyte)
        self.senders={};self.receivers={}
        for side,arm in [('left','A'),('right','B')]:
            send=getattr(self.lib,'OnSetChData'+arm)
            send.argtypes=[ptr,ctypes.c_long,ctypes.c_long];send.restype=ctypes.c_bool
            recv=getattr(self.lib,'OnGetChData'+arm)
            recv.argtypes=[ptr,ctypes.POINTER(ctypes.c_long)];recv.restype=ctypes.c_long
            self.senders[side],self.receivers[side]=send,recv
        try:
            if not self.lib.OnLinkTo(*[int(x) for x in self.host.split('.')]):
                raise RuntimeError('Vendor SDK connection unavailable or already owned')
            self.check_stationary()
        except BaseException:
            self.close();raise
        return self

    def check_stationary(self):
        buf=(ctypes.c_ubyte*2048)()
        if not self.lib.OnGetBuf(buf): raise RuntimeError('No vendor state feedback')
        raw=bytes(buf)
        state=struct.unpack_from('<6i',raw)
        if state[0]!=0 or state[3]!=0 or state[2] or state[5]:
            raise RuntimeError('Exclusive gripper test requires both arms idle and without error')
        for arm in range(2):
            serial=struct.unpack_from('<i',raw,760+312*arm)[0]
            velocity=struct.unpack_from('<7f',raw,792+312*arm)
            if not serial or any(not math.isfinite(v) or abs(v)>1 for v in velocity):
                raise RuntimeError('Robot is moving or feedback is invalid')

    def read_register(self,side,rid):
        if self.closed:raise RuntimeError('Reader closed')
        payload=query_bytes(self.config['motors'][side]['motor_id'],rid)
        data=(ctypes.c_ubyte*len(payload)).from_buffer_copy(payload)
        begin=time.monotonic_ns()
        if not self.senders[side](data,len(payload),1):
            raise RuntimeError('Vendor CAN send slot busy; another owner may be active')
        self.trace.append({'side':side,'can_id':0x7ff,'function':0x33,'register':rid})
        deadline=time.monotonic()+.2
        while time.monotonic()<deadline:
            buf=(ctypes.c_ubyte*256)();channel=ctypes.c_long(1)
            size=self.receivers[side](buf,ctypes.byref(channel))
            if size:
                if size!=12 or channel.value!=1: raise RuntimeError('Unexpected CAN response layout')
                raw=bytes(buf[:size])
                if struct.unpack_from('<I',raw)[0]!=0:raise RuntimeError('Unexpected CAN response ID')
                if raw[4:8]!=payload[4:8]:
                    # This endpoint repeats previous register echoes. Only skip
                    # recognized read-only replies; never reinterpret as current.
                    if raw[4:7]==b'\x01\x00\x33' and raw[7] in REGISTERS:
                        self.duplicate_replies+=1
                        continue
                    self.unexpected_reply={'side':side,'raw_hex':raw.hex(),'query_hex':payload.hex(),
                        'pc_reply_ns':time.monotonic_ns(),'cause':'Unknown: unsolicited/cached/corrupted response; ownership cannot be assumed'}
                    raise RuntimeError('Unsolicited CAN response: %s, query=%s; CAN ownership/freshness unverified'%(raw.hex(),payload.hex()))
                value=struct.unpack_from('<f',raw,8)[0]
                if not math.isfinite(value):raise RuntimeError('Nonfinite gripper reading')
                if rid in (0x50,0x51) and abs(value)>4*math.pi:raise RuntimeError('Implausible gripper position')
                valid=not(rid in (0x3D,0x3E) and value==0)
                return {'value':value if valid else None,'raw_value':value,
                        'pc_query_start_ns':begin,'pc_reply_ns':time.monotonic_ns(),
                        'register':rid,'source':'DM register read 0x33','valid':valid,
                        'unverified_reason':None if valid else 'zero_temperature_reply_not_verified'}
            time.sleep(.001)
        raise RuntimeError('%s gripper register 0x%02x reply timed out'%(side,rid))

    def sample(self,temperatures=False):
        self.check_stationary()
        motors={}
        for side in ('left','right'):
            fields={REGISTERS[r]:self.read_register(side,r) for r in ((0x50,0x51,0x3D,0x3E) if temperatures else (0x50,0x51))}
            if temperatures:
                self.temperature_cache[side]={k:fields[k] for k in ('driver_temperature_c','motor_temperature_c')}
            fields.update(self.temperature_cache[side])
            m=self.config['motors'][side]
            fraction=(fields['position_rad']['value']-m['open_rad'])/(m['closed_rad']-m['open_rad'])
            motors[side]={'fields':fields,'closure_fraction':max(0.,min(1.,fraction)) if -.01<=fraction<=1.01 else None,
                'closure_fraction_unclipped':fraction,'within_calibrated_travel':-.01<=fraction<=1.01,
                'calibration':{'open_rad':m['open_rad'],'closed_rad':m['closed_rad']},
                'velocity_rad_s':None,'torque_nm':None,'measured_width_m':None,
                'missing_fields_reason':'Not provided by these read-only registers; not inferred from commands'}
        return {'type':'grippers','motors':motors,'pc_sample_end_ns':time.monotonic_ns(),
                'timestamp_kind':'laptop_monotonic_CAN_reply','query_only':True}

    def close(self):
        if self.closed:return
        self.closed=True
        if self.lib is not None:self.lib.OnRelease()
        if self.lock is not None:self.lock.close()

    def __enter__(self):return self.connect()
    def __exit__(self,*args):self.close()

class GripperSampler(threading.Thread):
    def __init__(self,reader,hz=20):
        super().__init__(daemon=True)
        self.reader=reader;self.hz=hz;self.samples=[];self.error=None
        self.done=threading.Event();self.sink=None

    def run(self):
        start=time.monotonic();last_temp=-1
        try:
            tick=0
            while not self.done.is_set():
                if self.done.wait(max(0,start+tick/self.hz-time.monotonic())):break
                now=time.monotonic()
                if now-(start+tick/self.hz)>.1:raise RuntimeError('Gripper sampler over 100 ms late')
                # No catch-up flood if a read overruns its scheduled tick.
                tick=max(tick,int((now-start)*self.hz))
                temp=int(now-start)!=last_temp
                sample=self.reader.sample(temperatures=temp)
                if temp:last_temp=int(now-start)
                sample.update(sequence=len(self.samples),tick=tick)
                self.samples.append(sample)
                if self.sink:self.sink(sample)
                tick+=1
        except Exception as exc:self.error=str(exc)

    def stop(self):
        self.done.set();self.join(2)
        if self.is_alive():raise RuntimeError('Gripper reader did not stop')
