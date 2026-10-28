"""Pause offline encoding only after every submitted hardware frame drains."""
import os
from pathlib import Path
import time


def checkpoint(encoders):
    value = os.environ.get('GALAXIS_FINALIZER_PAUSE_FILE')
    if not value:
        return
    request = Path(value)
    if not request.exists():
        return
    # The producer is stopped here; encoder threads finish their accepted work.
    # Never SIGSTOP an MPP ioctl: the kernel may abort the hardware job.
    while any(encoder.accepted - encoder.count > (0 if encoder.native else 1) for encoder in encoders.values()):
        for encoder in encoders.values():
            if encoder.failure or encoder.process.poll() is not None:
                raise RuntimeError(encoder.failure or '视频编码器在暂停前退出')
        if not request.exists():
            return
        time.sleep(.01)
    acknowledgement = request.with_suffix('.ack')
    acknowledgement.touch()
    try:
        while request.exists():
            time.sleep(.02)
    finally:
        acknowledgement.unlink(missing_ok=True)
