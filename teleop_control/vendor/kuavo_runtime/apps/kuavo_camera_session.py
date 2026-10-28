"""AGX camera-only guardian sent over SSH stdin. Never starts ROS master/control.

Existing cameras are reused without modification. The only permitted launch is the
locally audited Orbbec color-camera file, verified by SHA256 before execution.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

LAUNCH_SHA256 = 'a884914b14732e0ccf0a1bce1a4e19212951c389c66981df433114b603a1a99b'
SERIAL = 'CP2H163000S6'
TOPIC = '/camera/color/image_raw'


def choose_action(fresh, existing_node, existing_publisher):
    if fresh:
        return 'reuse'
    if existing_node or existing_publisher:
        raise RuntimeError('existing camera node/publisher has no fresh frames; left untouched；已有相机无新帧，未重启')
    return 'start'


def verify_launch(path):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != LAUNCH_SHA256:
        raise RuntimeError('camera launch changed; audit required before automatic start')


def launch_command(path):
    return ['roslaunch', '--wait', str(path), 'camera_name:=camera', 'serial_number:=' + SERIAL,
            'color_width:=1280', 'color_height:=720', 'color_fps:=60',
            'enable_depth:=false', 'enable_left_ir:=false', 'enable_right_ir:=false',
            'enable_laser:=false', 'publish_tf:=false', 'enable_point_cloud:=false',
            'enable_colored_point_cloud:=false', 'enable_hardware_reset:=false']


def camera_usb_present(root=Path('/sys/bus/usb/devices')):
    for path in root.glob('*/serial'):
        try:
            if path.read_text().strip() == SERIAL:
                return True
        except OSError:
            pass
    return False


def main():
    import rosgraph
    import rospkg
    import rospy
    from sensor_msgs.msg import Image
    process = None
    lock = None
    stop = threading.Event()
    fresh = threading.Event()
    last_stamp = [None]
    last_message = [None]

    def on_image(message):
        stamp = message.header.stamp.to_nsec()
        if stamp > 0 and last_stamp[0] is not None and stamp > last_stamp[0]:
            fresh.set()
        last_stamp[0] = stamp
        last_message[0] = message

    def interrupted(*_):
        stop.set()

    try:
        # Fail if manufacturer ROS Master is absent; never launch roscore.
        master = rosgraph.Master('/galaxis_camera_precheck')
        master.getPid()
        rospy.init_node('galaxis_camera_precheck', anonymous=True, disable_signals=True)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, interrupted)
        subscription = rospy.Subscriber(TOPIC, Image, on_image, queue_size=1, buff_size=16*1024*1024)
        fresh.wait(3)
        state = master.getSystemState()
        nodes = {node for entries in state for _, owners in entries for node in owners}
        publishers = any(topic == TOPIC and owners for topic, owners in state[0])
        action = choose_action(fresh.is_set(), '/camera/camera' in nodes, publishers)
        if action == 'reuse':
            message = last_message[0]
            print(f'KUAVO_CAMERA_SESSION_READY reused=True size={message.width}x{message.height}', flush=True)
            return 0
        lock = os.open('/tmp/galaxis-head-camera-' + str(os.getuid()) + '.lock',
                       os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(lock)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('invalid camera session lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        launch = Path(rospkg.RosPack().get_path('orbbec_camera')) / 'launch/gemini_330_series.launch'
        verify_launch(launch)
        if not camera_usb_present():
            raise RuntimeError('Gemini 335L USB serial CP2H163000S6 is absent；检查相机USB与供电')
        # Recheck registration after acquiring our lock to avoid duplicate launch.
        state = master.getSystemState()
        nodes = {node for entries in state for _, owners in entries for node in owners}
        choose_action(False, '/camera/camera' in nodes,
                      any(topic == TOPIC and owners for topic, owners in state[0]))
        if stop.is_set():
            return 0
        print('KUAVO_CAMERA_SESSION_START camera_only=True requested=1280x720@60', flush=True)
        process = subprocess.Popen(launch_command(launch), stdin=subprocess.DEVNULL,
                                   stdout=sys.stderr, stderr=sys.stderr, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 25
        while not stop.is_set() and not fresh.wait(0.2):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('camera launch produced no fresh image; inspect driver log above')
        if stop.is_set():
            return 0
        message = last_message[0]
        if (message.width, message.height) != (1280, 720):
            raise RuntimeError('camera did not select requested 1280x720; no silent resolution fallback')
        print(f'KUAVO_CAMERA_SESSION_READY reused=False size={message.width}x{message.height}', flush=True)
        # SSH disconnection closes output: BrokenPipe triggers finally and stops only our launch.
        while not stop.wait(1):
            if process.poll() is not None:
                raise RuntimeError('owned camera launch exited')
            print('KUAVO_CAMERA_SESSION_ALIVE camera_only=True', flush=True)
        return 0
    except Exception as error:
        try:
            print(f'KUAVO_CAMERA_SESSION_ERROR reason={error}', flush=True)
        except (BrokenPipeError, OSError):
            pass
        return 2
    finally:
        if process is not None and process.poll() is None:
            for number, timeout in ((signal.SIGINT, 6), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
                try:
                    os.killpg(process.pid, number)
                    process.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except ProcessLookupError:
                    break
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
