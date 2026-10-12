"""File 1/4 — RECEIVE: raw Pico controller state via xrobotoolkit_sdk.

No threads, no dedup, no backoff: the main loop polls read() directly.

ORDERING CONSTRAINT (hw-verified 2026-07-16): init() MUST run BEFORE
agibot_gdk is initialized in the same process — GDK-first segfaults.
Requires the XRoboToolkit PC service up (127.0.0.1:60061) with the headset
connected, and the process must exit via os._exit() to sidestep the SDK's
teardown abort (send.py does both).
"""
import time

STALE_S = 0.3  # no advancing device timestamp for this long => stale


class Controllers:
    def __init__(self):
        self._xrt = None
        self._last_ts = 0
        self._last_advance = None

    def init(self):
        """Import + init the XR SDK. Call before any GDK init. Raises if the
        PC service is down."""
        import xrobotoolkit_sdk as xrt

        xrt.init()
        self._xrt = xrt

    def read(self):
        """Latest controller sample, or None until first headset data.

        {"stale": bool,
         "head": [x,y,z,qx,qy,qz,qw],                   # XR world frame
         "left"/"right": {"pose": [x,y,z,qx,qy,qz,qw],  # XR world frame
                          "grip": 0..1, "trigger": 0..1}}
        """
        x = self._xrt
        now = time.monotonic()
        try:
            ts = int(x.get_time_stamp_ns())
        except Exception:
            ts = 0
        if ts > self._last_ts:  # ts==0 means service up but no headset yet
            self._last_ts = ts
            self._last_advance = now
        if self._last_advance is None:
            return None
        return {
            "stale": (now - self._last_advance) > STALE_S,
            "head": [float(v) for v in x.get_headset_pose()],
            "left": {
                "pose": [float(v) for v in x.get_left_controller_pose()],
                "grip": float(x.get_left_grip()),
                "trigger": float(x.get_left_trigger()),
            },
            "right": {
                "pose": [float(v) for v in x.get_right_controller_pose()],
                "grip": float(x.get_right_grip()),
                "trigger": float(x.get_right_trigger()),
            },
            # Button functions are available in xrobotoolkit_sdk 1.0.2.  Keep
            # them separate from the analog controls so callers can edge-
            # detect a press without changing the arm clutch behaviour.
            "buttons": {
                "A": _button(x, "get_A_button"),
                "B": _button(x, "get_B_button"),
                "X": _button(x, "get_X_button"),
                "Y": _button(x, "get_Y_button"),
                "left_menu": _button(x, "get_left_menu_button"),
                "right_menu": _button(x, "get_right_menu_button"),
                "left_axis": _button(x, "get_left_axis_click"),
                "right_axis": _button(x, "get_right_axis_click"),
            },
        }

    def close(self):
        if self._xrt is not None:
            try:
                self._xrt.close()
            except Exception:
                pass
            self._xrt = None


def _button(xrt, name):
    """Read one optional SDK button without losing the whole XR sample."""
    fn = getattr(xrt, name, None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False
