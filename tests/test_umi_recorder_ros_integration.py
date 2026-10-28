"""Exercise the production raw ROS callback, queued writes and stop/save path."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
rclpy=pytest.importorskip('rclpy')
from rclpy.node import Node
from sensor_msgs.msg import JointState
from mcap.reader import NonSeekingReader


def test_actual_raw_subscription_records_dds_times_and_drains_on_stop(tmp_path):
    package=Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'
    env=dict(os.environ,ROS_DOMAIN_ID='204',PYTHONPATH=str(package)+os.pathsep+os.environ.get('PYTHONPATH',''))
    # Parent and child are isolated from any real collection domain.
    old_domain=os.environ.get('ROS_DOMAIN_ID');os.environ['ROS_DOMAIN_ID']='204'
    rclpy.init();node=Node('queue_smoke_publisher');pub=node.create_publisher(JointState,'/joint_states',100)
    root=tmp_path/'actual-raw';log=tmp_path/'recorder.log'
    with log.open('w') as out:
        proc=subprocess.Popen([sys.executable,'-m','galaxis_core.compressed_recorder','--defer-video','-o',str(root),'/joint_states'],env=env,stdout=out,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+15
            while pub.get_subscription_count()==0:
                assert proc.poll() is None,log.read_text()
                assert time.monotonic()<deadline,log.read_text()
                time.sleep(.05)
            for i in range(100):
                msg=JointState();msg.header.stamp=node.get_clock().now().to_msg();msg.name=['left_left_finger_joint'];msg.position=[i*.001]
                pub.publish(msg);rclpy.spin_once(node,timeout_sec=.01)
            time.sleep(.3);proc.send_signal(signal.SIGINT);assert proc.wait(timeout=15)==0,log.read_text()
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            node.destroy_node();rclpy.shutdown()
            if old_domain is None:os.environ.pop('ROS_DOMAIN_ID',None)
            else:os.environ['ROS_DOMAIN_ID']=old_domain
    marker=json.loads((root/'.deferred-raw.json').read_text())
    with (root/marker['file']).open('rb') as stream:
        messages=[m for _,_,m in NonSeekingReader(stream,validate_crcs=True).iter_messages(log_time_order=False)]
    assert len(messages)==100
    assert any(m.publish_time!=m.log_time for m in messages)
    report=json.loads((root/'capture-transport.json').read_text())
    assert report['writer_queue']['accepted']==report['writer_queue']['written']==100
    assert report['writer_queue']['pending_bytes']==0
    assert report['topics']['/joint_states']['samples']==100
