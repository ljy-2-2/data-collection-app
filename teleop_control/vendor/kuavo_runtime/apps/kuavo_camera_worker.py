"""Standalone SSH worker: subscribe to an existing ROS camera; never control motion.

Sent via SSH stdin to python3; no deployment or robot filesystem writes needed.
ROS/OpenCV imports occur only in main. stdout is reserved for the GVID binary stream.
"""

import argparse
import math
import struct
import sys
import threading
import time


CAMERA_NAME = b'kuavo5w-head'
HEADER = struct.Struct('!4sBBHQQQIII')


class WorkerStats:
    """Local work durations distinguish compression from SSH backpressure."""

    def __init__(self):
        self.frames = 0
        self.max_convert = self.max_encode = self.max_send = 0.0

    def record(self, started, converted, encoded, sent):
        self.frames += 1
        self.max_convert = max(self.max_convert, converted - started)
        self.max_encode = max(self.max_encode, encoded - converted)
        self.max_send = max(self.max_send, sent - encoded)

    def format(self, incoming, overwritten):
        return (f'frames={self.frames} images_received={incoming} images_overwritten={overwritten} '
                f'max_convert_ms={self.max_convert * 1000:.3f} '
                f'max_encode_ms={self.max_encode * 1000:.3f} '
                f'max_send_ms={self.max_send * 1000:.3f}')


class FramePacer:
    """Limit frame starts, not work duration plus a second full frame period."""

    def __init__(self, fps):
        self.period = 1.0 / fps
        self.next_send = 0.0

    def wait_seconds(self, now):
        return max(0.0, self.next_send - now)

    def mark_started(self, now):
        # If overloaded, do not accumulate deadlines or burst through old frames.
        self.next_send = now + self.period


def pack_jpeg_frame(jpeg, width, height, stamp_ns, sequence):
    if not isinstance(jpeg, bytes) or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
        raise ValueError('expected a complete JPEG')
    if len(jpeg) > 8 * 1024 * 1024:
        raise ValueError('JPEG exceeds frame size limit')
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 8192
           for value in (width, height)):
        raise ValueError('invalid frame dimensions')
    if not isinstance(stamp_ns, int) or stamp_ns <= 0 or stamp_ns >= 2**64:
        raise ValueError('invalid source timestamp')
    if not isinstance(sequence, int) or sequence < 0 or sequence >= 2**64:
        raise ValueError('invalid frame sequence')
    return HEADER.pack(b'GVID', 1, 1, len(CAMERA_NAME), sequence,
                       stamp_ns, time.monotonic_ns(), width, height, len(jpeg)) + CAMERA_NAME + jpeg


def main():
    parser = argparse.ArgumentParser(description='Read-only Kuavo ROS color camera worker')
    parser.add_argument('--topic', default='/camera/color/image_raw')
    parser.add_argument('--fps', type=float, default=30)
    parser.add_argument('--quality', type=int, default=80)
    parser.add_argument('--duration', type=float, default=300, help='Seconds; 0 runs until stopped')
    parser.add_argument('--frame-timeout', type=float, default=5)
    args = parser.parse_args()
    if (not math.isfinite(args.fps) or not 1 <= args.fps <= 60 or not 1 <= args.quality <= 100
            or not math.isfinite(args.duration) or args.duration < 0
            or not math.isfinite(args.frame_timeout) or args.frame_timeout <= 0):
        parser.error('invalid fps, quality, duration or frame timeout')

    wire = sys.stdout.buffer
    sys.stdout = sys.stderr  # ROS/library diagnostics must not corrupt the binary stream.
    import cv2
    import rospy
    from cv_bridge import CvBridge
    from sensor_msgs.msg import Image

    condition = threading.Condition()
    latest = [None]
    received = [time.monotonic()]
    incoming = [0]
    overwritten = [0]

    def on_image(message):
        with condition:
            incoming[0] += 1
            if latest[0] is not None:
                overwritten[0] += 1
            latest[0] = message  # newest-only: never build an image backlog.
            received[0] = time.monotonic()
            condition.notify()

    rospy.init_node('galaxis_camera_reader', anonymous=True, disable_signals=False)
    bridge = CvBridge()
    subscription = rospy.Subscriber(args.topic, Image, on_image, queue_size=1, buff_size=16 * 1024 * 1024)
    deadline = time.monotonic() + args.duration if args.duration else math.inf
    pacer = FramePacer(args.fps)
    stats = WorkerStats()
    next_stats = time.monotonic() + 5
    sequence = 0
    try:
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            with condition:
                now = time.monotonic()
                if now - received[0] > args.frame_timeout:
                    raise RuntimeError('camera topic has no fresh frames: ' + args.topic)
                wait_seconds = pacer.wait_seconds(now)
                if latest[0] is None or wait_seconds > 0:
                    condition.wait(timeout=min(0.1, max(0.001, wait_seconds)))
                    continue
                message = latest[0]
                latest[0] = None
                pacer.mark_started(now)
            image = bridge.imgmsg_to_cv2(message, desired_encoding='bgr8')
            converted = time.monotonic()
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
            encoded_at = time.monotonic()
            if not ok:
                raise RuntimeError('JPEG encoding failed')
            stamp = message.header.stamp.to_nsec()
            # Never disguise missing capture timestamps as valid synchronized data.
            wire.write(pack_jpeg_frame(encoded.tobytes(), int(image.shape[1]), int(image.shape[0]), stamp, sequence))
            wire.flush()
            sent_at = time.monotonic()
            stats.record(now, converted, encoded_at, sent_at)
            sequence += 1
            if sent_at >= next_stats:
                with condition:
                    counters = incoming[0], overwritten[0]
                print('KUAVO_CAMERA_WORKER_STATS ' + stats.format(*counters)
                      + ' physical_robot_command_sent=False', file=sys.stderr, flush=True)
                next_stats = sent_at + 5
    finally:
        subscription.unregister()
        print('KUAVO_CAMERA_WORKER_SUMMARY ' + stats.format(incoming[0], overwritten[0])
              + ' physical_robot_command_sent=False', file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('KUAVO_CAMERA_WORKER_ERROR ' + str(error), file=sys.stderr)
        sys.exit(2)
