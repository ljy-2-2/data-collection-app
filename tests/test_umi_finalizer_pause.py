import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'))
from galaxis_core.finalizer_pause import checkpoint


def test_hardware_pause_ack_waits_for_last_inflight_frame(tmp_path,monkeypatch):
    request=tmp_path/'pause.request';request.touch()
    monkeypatch.setenv('GALAXIS_FINALIZER_PAUSE_FILE',str(request))
    encoder=SimpleNamespace(native=True,count=2,accepted=3,failure=None,process=SimpleNamespace(poll=lambda:None))
    thread=threading.Thread(target=checkpoint,args=({'camera':encoder},));thread.start()
    try:
        time.sleep(.05);assert not request.with_suffix('.ack').exists()
        encoder.count=3
        deadline=time.monotonic()+2
        while not request.with_suffix('.ack').exists():
            assert time.monotonic()<deadline;time.sleep(.01)
        assert thread.is_alive()
    finally:request.unlink();thread.join(2)
    assert not thread.is_alive() and not request.with_suffix('.ack').exists()
