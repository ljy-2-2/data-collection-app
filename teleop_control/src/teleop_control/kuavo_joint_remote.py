"""Robot-side read-only 100 Hz sensor subscriber; no command topics or services."""
import hashlib
import json
import math
from pathlib import Path
import queue
import signal
import threading
import time
import xml.etree.ElementTree as ET


class SensorSampler:
    """Downsample source timestamps, never repeat/interpolate a measurement."""
    def __init__(self,version):
        self.version=version;self.next_stamp=None;self.last_stamp=None;self.sequence=0
        self.lock=threading.Lock()
    def receive(self,message):
        received=time.monotonic_ns()
        stamp=message.sensor_time.secs*1_000_000_000+message.sensor_time.nsecs
        with self.lock:
            if self.last_stamp is not None and stamp<self.last_stamp:raise ValueError('sensor clock moved backwards')
            if stamp==self.last_stamp:return None
            self.last_stamp=stamp
            if self.next_stamp is not None and stamp<self.next_stamp:return None
            self.next_stamp=(stamp if self.next_stamp is None else max(self.next_stamp,stamp-9_999_999))+10_000_000
            self.sequence+=1
            return dict(event='frame',robot_version=self.version,sensor_secs=message.sensor_time.secs,
                sensor_nsecs=message.sensor_time.nsecs,joint_q=list(message.joint_data.joint_q),
                joint_v=list(message.joint_data.joint_v),sequence=self.sequence,received_monotonic_ns=received)


def run():
    import rospy
    from kuavo_msgs.msg import sensorsData
    stopped=threading.Event();pending=queue.Queue(maxsize=512);failed=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,lambda *_:stopped.set())
    rospy.init_node('app_kuavo_readonly_state',anonymous=True,disable_signals=True,disable_rosout=True)
    version=str(rospy.get_param('/robot_version'))
    if version!='63':raise ValueError('只读采集仅支持已核对的 63 型机器人')
    xml=Path(rospy.get_param('/urdfFile')).read_bytes();root=ET.fromstring(xml)
    links=['knee_link','leg_link','waist_link','waist_yaw_link']+[f'zarm_{s}{i}_link' for s in ('l','r') for i in range(1,8)]+['zhead_1_link','zhead_2_link']
    names=[]
    for link in links:
        found=[j.get('name') for j in root.findall('joint') if j.find('child') is not None and j.find('child').get('link')==link]
        if len(found)!=1:raise ValueError('无法核验关节顺序')
        names.append(found[0])
    print(json.dumps(dict(event='ready',robot_version=version,joint_names=names,
                         urdf_sha256=hashlib.sha256(xml).hexdigest(),joint_hz=100)),flush=True)
    sampler=SensorSampler(version)
    def receive(message):
        try:
            item=sampler.receive(message)
            if item is not None:pending.put_nowait(item)
        except (ValueError,queue.Full):failed.set()
    sub=rospy.Subscriber('/sensors_data_raw',sensorsData,receive,queue_size=256,tcp_nodelay=True)
    heartbeat=time.monotonic()
    try:
        while not stopped.is_set() and not rospy.is_shutdown():
            if failed.is_set():raise RuntimeError('关节采集缓存溢出或源时间倒退，未补帧')
            try:item=pending.get(timeout=.02)
            except queue.Empty:item=None
            if item:
                item['source_age_ms']=(time.monotonic_ns()-item['received_monotonic_ns'])/1e6
                print(json.dumps(item,allow_nan=False),flush=True)
            if time.monotonic()-heartbeat>=.5:
                heartbeat=time.monotonic();print(json.dumps(dict(event='heartbeat',robot_version=version)),flush=True)
    finally:sub.unregister();rospy.signal_shutdown('read-only subscriber closed')

if __name__=='__main__':run()
