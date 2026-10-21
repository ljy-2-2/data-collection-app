"""Exercise durable queue, real OS handoff and real MP4 publication."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'))
sys.path.insert(0, str(ROOT / 'teleop_control/src'))
from galaxis_core.mp4_worker import BackgroundFinalizer, atomic_json, finalize_episode
from galaxis_core.recording_priority import CapturePriority
from teleop_control.umi_uploader import UmiRosbagUploader
from test_umi_mp4_bundle import recording, contract


def job(tmp_path, name='001'):
    folder = tmp_path / 'task' / name
    folder.parent.mkdir(exist_ok=True)
    source = recording(folder)
    contract(folder)
    atomic_json(folder / '.mp4-pending.json', {'author': 'operator-' + name, '_video_crf': 12})
    return folder, source


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'background handoff timed out'
        time.sleep(.01)


def test_capture_preempts_real_process_and_resume_after_recording(tmp_path, monkeypatch):
    folder, source = job(tmp_path)
    worker = BackgroundFinalizer(tmp_path, command_factory=lambda _: [sys.executable, '-c', 'import time; time.sleep(60)'])
    monkeypatch.setattr(worker, '_can_keep_encoder_memory', lambda: True)
    thread = threading.Thread(target=worker.run)
    thread.start()
    priority = CapturePriority(tmp_path)
    try:
        wait_until(lambda: worker.process is not None)
        first = worker.process
        started = time.monotonic()
        priority.acquire()
        assert time.monotonic() - started < 2
        assert first.poll() is None
        assert worker.paused_at is not None
        assert source.is_file() and (folder / '.mp4-pending.json').is_file()
        assert not (folder / 'recording-failure.json').exists()
        time.sleep(.15)
        assert worker.process is first
        before = worker.deadline
        priority.release()
        wait_until(lambda: worker.paused_at is None)
        assert worker.process.pid == first.pid
        assert worker.deadline >= before + .14
    finally:
        priority.release()
        worker.stop_event.set()
        thread.join(4)
    assert not thread.is_alive()
    assert source.is_file()  # service shutdown also preserves pending originals


def test_memory_pressure_releases_encoder_and_retries_from_source(tmp_path, monkeypatch):
    folder, source = job(tmp_path)
    worker = BackgroundFinalizer(tmp_path, command_factory=lambda _: [sys.executable, '-c', 'import time; time.sleep(60)'])
    monkeypatch.setattr(worker, '_can_keep_encoder_memory', lambda: False)
    worker.tick()
    first = worker.process
    intent = __import__('galaxis_core.recording_priority', fromlist=['open_lock'])
    with intent.open_lock(tmp_path, '.capture-intent.lock') as lock:
        assert intent.try_lock(lock)
        worker.tick()
        assert first.poll() is not None and worker.process is None
    assert source.exists() and (folder / '.mp4-pending.json').exists()
    assert not (folder / 'recording-failure.json').exists()


def test_paused_group_stops_children_and_shutdown_cleans_up(tmp_path, monkeypatch):
    folder, _ = job(tmp_path)
    counter = tmp_path / 'counter'
    code = ('import pathlib,time; p=pathlib.Path(' + repr(str(counter)) + '); '
            '\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.01)')
    parent = 'import subprocess,sys; subprocess.run([sys.executable,"-c",' + repr(code) + '])'
    worker = BackgroundFinalizer(tmp_path, command_factory=lambda _: [sys.executable, '-c', parent])
    monkeypatch.setattr(worker, '_can_keep_encoder_memory', lambda: True)
    worker.tick()
    try:
        wait_until(counter.exists)
        worker._pause_conversion()
        time.sleep(.05)
        paused = counter.read_text()
        time.sleep(.1)
        assert counter.read_text() == paused
        assert UmiRosbagUploader._processing(folder)
        worker.tick()
        wait_until(lambda: counter.read_text() != paused)
        worker._pause_conversion()
        proc = worker.process
    finally:
        worker._stop_conversion()
    assert proc.poll() is not None
    assert not (folder / '.mp4-converting').exists()


def test_failed_encoder_recovers_raw_then_continues_next_job(tmp_path, monkeypatch):
    first, source = job(tmp_path, '001')
    second, _ = job(tmp_path, '002')
    import os
    monkeypatch.setenv('PYTHONPATH', str(ROOT/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core') + os.pathsep + os.environ.get('PYTHONPATH', ''))
    worker = BackgroundFinalizer(tmp_path, command_factory=lambda folder: [sys.executable, '-c', 'raise SystemExit(7)' if folder == first else 'import time; time.sleep(60)'])
    thread = threading.Thread(target=worker.run); thread.start()
    try:
        wait_until(lambda: worker.folder == second, timeout=8)
        assert source.exists() and (first/'recovered.mcap').exists()
        assert not UmiRosbagUploader._processing(first)
        meta = json.loads((first/'meta.json').read_text())
        assert meta['interruption']['recovered'] and meta['episode']['success'] is False
    finally:
        worker.stop_event.set(); thread.join(4)


def test_finalization_preserves_author_timestamps_and_crf12_then_allows_upload(tmp_path):
    folder, source = job(tmp_path)
    finalize_episode(folder)
    meta = json.loads((folder / 'meta.json').read_text())
    manifest = json.loads((folder / 'media/video-manifest.json').read_text())
    assert meta['author'] == 'operator-001' and meta['recording_format'] == 'umi-mp4-v1'
    assert manifest['crf'] == 12
    assert all(c['frame_count'] == 5 for c in manifest['cameras'])
    assert not source.exists() and not (folder / '.mp4-pending.json').exists()
    assert not UmiRosbagUploader._processing(folder)
    # Crash after metadata publication/source cleanup but before pending unlink:
    atomic_json(folder / '.mp4-pending.json', {'author': 'operator-001', '_video_crf': 12})
    finalize_episode(folder)
    assert not (folder / '.mp4-pending.json').exists()


def test_retry_cleans_only_owned_partial_encodes(tmp_path):
    from galaxis_core.video_bundle import FORMAT
    folder, source = job(tmp_path)
    partial = folder / '.mp4-abc12345'; partial.mkdir()
    (partial / '.encoding-stage').write_text(FORMAT)
    (partial / 'left.mp4').write_bytes(b'incomplete encode')
    unrelated = folder / '.mp4-xyz12345'; unrelated.mkdir()
    (unrelated / 'notes.txt').write_text('keep')
    linked = folder / '.mp4-link1234'; linked.symlink_to(unrelated, target_is_directory=True)
    # Failed data validation must keep the original even while reclaiming only
    # disposable output left by a killed encoder.
    selected = json.loads((folder / 'camera-contract.json').read_text())
    selected['cameras']['left']['image_width'] = 1920
    atomic_json(folder / 'camera-contract.json', selected)
    with pytest.raises(ValueError, match='实际图像尺寸'):
        finalize_episode(folder)
    assert not partial.exists()
    assert source.exists() and (folder / '.mp4-pending.json').exists()
    assert (unrelated / 'notes.txt').read_text() == 'keep' and linked.is_symlink()


@pytest.mark.parametrize('marker', ['.mp4-pending.json', '.mp4-converting', 'recording-failure.json'])
def test_uploader_never_touches_pending_or_failed_episode(tmp_path, marker):
    folder = tmp_path / 'rosbags/task/001'; folder.mkdir(parents=True)
    (folder / 'meta.json').write_text('{}')
    (folder / 'metadata.yaml').write_text('invalid but must not even be read')
    (folder / marker).write_text('{}')
    client = Mock()
    uploader = UmiRosbagUploader(tmp_path, 'https://unused.test', client=client)
    uploader.scan_once(); uploader._upload(folder, {})
    assert not client.mock_calls


def test_failed_validation_preserves_source_and_pending(tmp_path):
    folder, source = job(tmp_path)
    selected = json.loads((folder / 'camera-contract.json').read_text())
    selected['cameras']['left']['image_width'] = 1920
    atomic_json(folder / 'camera-contract.json', selected)
    with pytest.raises(ValueError, match='实际图像尺寸'):
        finalize_episode(folder)
    assert source.exists() and (folder / '.mp4-pending.json').exists()
    assert not (folder / 'media').exists()


def test_efficiency_cpu_detection_is_portable(tmp_path):
    from galaxis_core.mp4_worker import efficiency_cpus
    for cpu, capacity in [(2, 400), (4, 400), (6, 1000), (8, 1024)]:
        path = tmp_path / f'cpu{cpu}'
        path.mkdir()
        (path / 'cpu_capacity').write_text(str(capacity))
    assert efficiency_cpus({2, 4, 6, 8}, tmp_path) == {2, 4}
    assert efficiency_cpus({6, 8}, tmp_path) == {6}
    assert efficiency_cpus({0, 1}, tmp_path) == {0}


def test_monitoring_reserves_fast_cpus_then_restores_idle_processing(tmp_path, monkeypatch):
    import fcntl
    from galaxis_core.recording_priority import open_lock
    worker = BackgroundFinalizer(tmp_path)
    worker._allowed_cpus = {0, 1, 2, 3}
    worker._efficiency_cpus = {0, 1}
    worker._applied_cpus = {0, 1, 2, 3}
    masks = []
    monkeypatch.setattr(os, 'sched_setaffinity', lambda pid, mask: masks.append((pid, set(mask))))
    with open_lock(tmp_path, '.capture-monitor.lock') as monitor:
        fcntl.flock(monitor, fcntl.LOCK_SH)
        worker._schedule_processing()
        worker._schedule_processing()
    worker._schedule_processing()
    assert masks == [(0, {0, 1}), (0, {0, 1, 2, 3})]
