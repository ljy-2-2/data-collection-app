"""XRoboToolkit acquisition only. Never imports or connects a robot SDK."""

import json
import signal
import time


def main():
    import xrobotoolkit_sdk as sdk

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    initialized = False
    try:
        sdk.init()
        initialized = True
        last_stamp = None
        while running:
            captured = time.monotonic()
            stamp = int(sdk.get_time_stamp_ns())
            if stamp > 0 and stamp != last_stamp:
                data = {
                    "timeStampNs": stamp,
                    "captured_monotonic_s": captured,
                    "Head": {"pose": list(sdk.get_headset_pose())},
                    "Controller": {},
                }
                for side, button in (("left", "Y"), ("right", "B")):
                    data["Controller"][side] = {
                        "pose": list(getattr(sdk, f"get_{side}_controller_pose")()),
                        "grip": float(getattr(sdk, f"get_{side}_grip")()),
                        "trigger": float(getattr(sdk, f"get_{side}_trigger")()),
                        "secondaryButton": bool(getattr(sdk, f"get_{button}_button")()),
                    }
                    primary = getattr(sdk, "get_A_button" if side == "right" else "get_X_button", None)
                    if primary is not None:
                        data["Controller"][side]["primaryButton"] = bool(primary())
                # Reject a mixture of frames if the shared service updated during reads.
                if int(sdk.get_time_stamp_ns()) == stamp:
                    print("MARVIN_XR " + json.dumps(data, allow_nan=False), flush=True)
                    last_stamp = stamp
            time.sleep(0.001)
    except Exception as error:
        print("MARVIN_XR " + json.dumps({"error": str(error)}), flush=True)
        return 1
    finally:
        if initialized:
            sdk.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
