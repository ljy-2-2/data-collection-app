"""Storage stalls must not block receive callbacks or discard accepted samples."""
import importlib.util
from pathlib import Path
import threading
import pytest

path=Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core/galaxis_core/recording_queue.py'
spec=importlib.util.spec_from_file_location('_umi_recording_queue_test',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
RecordingQueue=module.RecordingQueue


def test_stalled_writer_keeps_all_frames_and_original_timestamps_in_order():
    entered=threading.Event();release=threading.Event();written=[]
    def write(item):
        entered.set();assert release.wait(2);written.append(item)
    q=RecordingQueue(write,max_bytes=100,max_items=10)
    samples=[('camera',b'frame',123+i,120+i,i) for i in range(5)]
    try:
        q.submit(samples[0],5);assert entered.wait(1)
        for item in samples[1:]:q.submit(item,5)
        assert not written  # All submissions returned while storage was stalled.
    finally:
        release.set();q.close()
    assert written==samples
    assert q.bytes==0


def test_queue_overflow_is_explicit_and_accepted_data_still_drain():
    entered=threading.Event();release=threading.Event();written=[]
    def write(item):entered.set();assert release.wait(2);written.append(item)
    q=RecordingQueue(write,max_bytes=8)
    try:
        q.submit(b'aaaa',4);assert entered.wait(1);q.submit(b'bbbb',4)
        with pytest.raises(BufferError):q.submit(b'c',1)
    finally:release.set();q.close()
    assert written==[b'aaaa',b'bbbb']


def test_disk_error_propagates_to_collector_and_shutdown():
    def fail(_):raise OSError('disk disconnected')
    q=RecordingQueue(fail);q.submit(b'x',1)
    with pytest.raises(RuntimeError) as err:q.close()
    assert isinstance(err.value.__cause__,OSError)
    with pytest.raises(RuntimeError):q.submit(b'y',1)
