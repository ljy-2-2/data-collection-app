"""Durable offline MP4 queue, independent of ROS sessions and cloud upload."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .recording_priority import open_lock, try_lock


def efficiency_cpus(allowed, sysroot=Path('/sys/devices/system/cpu')):
    """Discover heterogeneous CPUs; never assume RK3588 CPU numbering."""
    try:
        capacities = {cpu: int((sysroot / f'cpu{cpu}/cpu_capacity').read_text()) for cpu in allowed}
        low, high = min(capacities.values()), max(capacities.values())
        if low > 0 and high >= low * 1.5:
            return {cpu for cpu, capacity in capacities.items() if capacity <= low * 1.1}
    except (OSError, ValueError):
        pass
    # Homogeneous computers also reserve CPU for live capture/preview.
    return set(sorted(allowed)[:max(1, len(allowed) // 2)])


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def finalize_episode(folder):
    from .video_bundle import convert_bundle, cleanup_source, cleanup_interrupted_encodes
    from .ownership import normalize_tree_access, resolve_record_owner
    folder = Path(folder)
    pending = folder / '.mp4-pending.json'
    meta = json.loads(pending.read_text())
    crf = meta.pop('_video_crf', 14)
    cleanup_interrupted_encodes(folder)
    started = time.monotonic()
    manifest = convert_bundle(folder, crf=crf)
    converted = time.monotonic()
    meta.update(files=[{'type': 'mcap', 'name': 'media/signals.mcap'}],
                recording_format='umi-mp4-v1', video_manifest='media/video-manifest.json')
    atomic_json(folder / 'meta.json', meta)
    # Every frame and timestamp has been validated before the original is
    # removed. Upload is gated on removal of the pending marker, below.
    cleanup_source(folder, manifest)
    (folder / 'recording-failure.json').unlink(missing_ok=True)
    normalize_tree_access(str(folder), *resolve_record_owner(str(folder)))
    pending.unlink()
    print(json.dumps(dict(stage='mp4_complete', episode=folder.name,
                          conversion_seconds=converted-started,
                          finalize_seconds=time.monotonic()-started), ensure_ascii=False), flush=True)


class BackgroundFinalizer:
    def __init__(self, root, *, command_factory=None):
        self.root = Path(root)
        self.stop_event = threading.Event()
        self.command_factory = command_factory or (
            lambda folder: [sys.executable, '-m', 'galaxis_core.mp4_worker', '--episode', str(folder)])
        self.process = None
        self.folder = None
        self.log = None
        self.resources = None
        self.started = 0.
        self.deadline = 0.
        self.paused_at = None
        self._allowed_cpus = set(os.sched_getaffinity(0))
        self._efficiency_cpus = efficiency_cpus(self._allowed_cpus)
        self._applied_cpus = self._allowed_cpus

    def _schedule_processing(self):
        if self._efficiency_cpus == self._allowed_cpus:
            return
        with open_lock(self.root, '.capture-monitor.lock') as lock:
            wanted = self._allowed_cpus if try_lock(lock) else self._efficiency_cpus
        if wanted == self._applied_cpus:
            return
        # New encoders inherit this mask; existing encoders and FFmpeg threads
        # are changed only inside this worker's own subprocess group.
        os.sched_setaffinity(0, wanted)
        if self.process is not None and self.process.poll() is None:
            for entry in Path('/proc').glob('[0-9]*'):
                try:
                    if os.getpgid(int(entry.name)) != self.process.pid:
                        continue
                    for thread in (entry / 'task').iterdir():
                        os.sched_setaffinity(int(thread.name), wanted)
                except (ProcessLookupError, FileNotFoundError):
                    pass
        self._applied_cpus = wanted

    @staticmethod
    def _can_keep_encoder_memory():
        # Preserve progress on machines with enough free RAM. Under memory
        # pressure retain the original terminate/retry behavior for capture.
        try:
            available = next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:'))
            return int(available) >= 2 * 1024 * 1024
        except (OSError, ValueError, StopIteration):
            return False

    def _pause_conversion(self):
        if self.process is None or self.process.poll() is not None:
            # tick() must still inspect a completed process's exit status.
            self._release_resources()
            return
        if not self._can_keep_encoder_memory():
            self._stop_conversion()
            return
        if self.paused_at is None:
            try:
                os.killpg(self.process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                return
            self.paused_at = time.monotonic()
        self._release_resources()

    def _release_resources(self):
        if self.resources is not None:
            self.resources.close()
            self.resources = None

    def _stop_conversion(self):
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                if self.paused_at is not None:
                    os.killpg(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            try:
                process.wait(1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(1)
        self._clear_process()

    def _clear_process(self):
        if self.log:
            self.log.close()
            self.log = None
        if self.folder:
            (self.folder / '.mp4-converting').unlink(missing_ok=True)
        self.process = None
        self.folder = None
        self.paused_at = None
        self._release_resources()

    def _fail(self, reason):
        folder = self.folder
        self._stop_conversion()
        if folder and folder.is_dir():
            atomic_json(folder / 'recording-failure.json', {'reason': reason, 'stage': 'mp4'})

    def tick(self):
        self._schedule_processing()
        with open_lock(self.root, '.capture-intent.lock') as intent:
            if not try_lock(intent):
                # Stop all encoder CPU/I/O, retaining progress when RAM allows.
                # Completed bundles can still upload in the independent uploader.
                self._pause_conversion()
                return
            if self.process is not None:
                if self.paused_at is not None:
                    self.resources = open_lock(self.root, '.video-processing.lock')
                    if not try_lock(self.resources):
                        self._release_resources()
                        return
                    self.deadline += time.monotonic() - self.paused_at
                    self.paused_at = None
                    try:
                        os.killpg(self.process.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                code = self.process.poll()
                if code is not None:
                    if code:
                        detail = (self.folder / '.mp4-finalize.log').read_text(errors='replace')[-1500:]
                        self._fail(f'MP4 后台保存失败（退出码 {code}），原始数据保留：{detail}')
                    else:
                        self._clear_process()
                elif time.monotonic() > self.deadline:
                    self._fail('MP4 后台保存超时，原始数据保留，可重试处理')
                return
            pending = next((p for p in sorted(self.root.glob('*/*/.mp4-pending.json'))
                            if not (p.parent / 'recording-failure.json').exists()), None)
            if pending is None:
                return
            self.resources = open_lock(self.root, '.video-processing.lock')
            if not try_lock(self.resources):
                self._release_resources()
                return
            from .video_bundle import conversion_timeout
            self.folder = pending.parent
            self.deadline = time.monotonic() + max(900, 4 * conversion_timeout(self.folder))
            # A retry should report its own failure, not the traceback from a
            # previous intentional interruption. Bound repeated retry storage.
            self.log = (self.folder / '.mp4-finalize.log').open('w')
            (self.folder / '.mp4-converting').touch()
            try:
                self.process = subprocess.Popen(self.command_factory(self.folder),
                    stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
                    env={**os.environ, 'GALAXIS_MANAGED_FINALIZER': '1'})
            except Exception:
                self._clear_process()
                raise

    def run(self):
        with open_lock(self.root, '.mp4-worker.lock') as singleton:
            if not try_lock(singleton):
                raise RuntimeError('Another MP4 finalizer already owns this data directory')
            try:
                while not self.stop_event.is_set():
                    self.tick()
                    self.stop_event.wait(.05)
            finally:
                self._stop_conversion()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--episode', type=Path)
    args = parser.parse_args()
    if args.episode:
        def interrupt(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupt)
        finalize_episode(args.episode)
        return
    if not args.root:
        parser.error('--root or --episode is required')
    # All encoder children inherit this priority. Let live camera/PICO receive
    # threads run first even BETWEEN rounds, while encoding uses spare CPU.
    os.nice(10)
    worker = BackgroundFinalizer(args.root)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop_event.set())
    worker.run()


if __name__ == '__main__':
    main()
