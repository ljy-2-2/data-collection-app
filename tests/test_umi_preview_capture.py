"""The device-check display may shrink without changing the capture contract."""
from io import BytesIO
from pathlib import Path
import sys
import threading

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'teleop_control/src'))
from teleop_control import umi
from teleop_control.umi_preview import MjpegFrames


@pytest.mark.parametrize('capture_size,display_size', [
    ((1920, 1080), (960, 540)), ((1280, 1024), (960, 768)),
    ((320, 240), (320, 240)),
])
def test_hardware_preview_preserves_source_mode_and_pixels(monkeypatch, capture_size, display_size):
    import cv2
    width, height = capture_size
    source = np.full((height, width, 3), (40, 100, 210), dtype=np.uint8)
    original = source.copy()
    settings, released = {}, threading.Event()

    class Capture:
        def isOpened(self): return True
        def set(self, key, value): settings[key] = value; return True
        def get(self, key): return 30.0
        def read(self):
            # A real input runs at 30 Hz. This fake returns the very same array
            # so any accidental in-place resizing/modification is detectable.
            released.wait(1.0 / 30)
            return True, source
        def release(self): released.set()

    monkeypatch.setattr(cv2, 'VideoCapture', lambda *_: Capture())
    mode = dict(width=width, height=height, fps=30.0, fourcc='MJPG')
    worker = umi._CameraPreviewWorker('/dev/nonexistent-preview-test', lambda _: None, mode)
    subscription = worker.subscribe()
    try:
        assert worker.wait_for_frame(3.0), worker.error
        encoded = MjpegFrames().feed(next(subscription))
        with Image.open(BytesIO(encoded)) as image:
            image.load()
            assert image.size == display_size
            red, green, blue = image.getpixel((10, 10))
            assert abs(red-210) < 4 and abs(green-100) < 4 and abs(blue-40) < 4
        assert settings[cv2.CAP_PROP_FRAME_WIDTH] == width
        assert settings[cv2.CAP_PROP_FRAME_HEIGHT] == height
        assert settings[cv2.CAP_PROP_FPS] == 30
        assert np.array_equal(source, original)
        assert worker._mode == mode
    finally:
        subscription.close()
        worker.stop()
    assert released.is_set()


def test_30hz_source_keeps_24hz_preview_cadence_without_lowering_capture_rate(monkeypatch):
    """Use a deterministic device clock; resetting each deadline causes 15 Hz."""
    import cv2

    source = np.full((24, 32, 3), 100, dtype=np.uint8)
    now = [1000.0]
    source_frames = []
    display_frames = []
    settings = {}
    released = []
    encode = cv2.imencode

    class Capture:
        def isOpened(self): return True
        def set(self, key, value): settings[key] = value; return True
        def get(self, key): return 30.0
        def read(self):
            if len(source_frames) == 300:
                worker._stop.set()
                return False, None
            # Ten seconds of hardware frames arrive at exactly 30 Hz. The
            # production loop must keep draining ALL of them, including those
            # that are intentionally not encoded for the display.
            now[0] = 1000.0 + (len(source_frames) + 1) / 30.0
            source_frames.append(now[0])
            return True, source
        def release(self): released.append(True)

    def track_encode(*args, **kwargs):
        display_frames.append(now[0])
        return encode(*args, **kwargs)

    monkeypatch.setattr(cv2, 'VideoCapture', lambda *_: Capture())
    monkeypatch.setattr(cv2, 'imencode', track_encode)
    monkeypatch.setattr(umi.time, 'monotonic', lambda: now[0])
    # Run the real worker loop synchronously so thread scheduling and wall
    # time cannot make this cadence regression intermittent.
    monkeypatch.setattr(umi.threading.Thread, 'start', lambda _: None)
    mode = dict(width=32, height=24, fps=30.0, fourcc='MJPG')
    worker = umi._CameraPreviewWorker('/dev/nonexistent-preview-test', lambda _: None, mode)
    worker._run()

    assert len(source_frames) == 300
    assert 239 <= len(display_frames) <= 241
    assert worker._sequence == len(display_frames)
    # Every complete interior second should receive 24 fresh display frames;
    # this catches bursts followed by starvation as well as the 15 Hz defect.
    for second in range(1, 9):
        count = sum(1000 + second <= value < 1001 + second for value in display_frames)
        assert 23 <= count <= 25, (second, count)
    assert max(b - a for a, b in zip(display_frames, display_frames[1:])) <= 2 / 30 + 1e-9
    assert settings[cv2.CAP_PROP_FPS] == 30.0
    assert worker._mode == mode
    assert released == [True]
