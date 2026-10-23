"""Bounded asynchronous JPEG -> H.264 encoder, without a raw disk spool."""
import collections
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time

from .mcap_video import validate_packet


class LiveEncoder:
    def __init__(self, callback, *, native=None, managed=False):
        self.managed = managed
        self.callback = callback
        self.native = native
        self.input = queue.Queue(maxsize=30)
        self.pending = queue.Queue(maxsize=60)
        self.errors = collections.deque(maxlen=20)
        self.failure = None
        self.count = 0
        self.accepted = 0
        self.last_output = time.monotonic()
        self.started = None
        self.closed = False
        if native:
            command = [str(native), '20']
        else:
            command = ['ffmpeg', '-hide_banner', '-v', 'error', '-threads', '1',
                       '-filter_threads', '1', '-probesize', '32', '-analyzeduration', '0',
                       '-f', 'mjpeg', '-framerate', '30', '-i', 'pipe:0', '-an',
                       '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast',
                       '-tune', 'zerolatency', '-crf', '18', '-pix_fmt', 'yuv420p',
                       '-x264-params', 'aud=1:repeat-headers=1:bframes=0:keyint=30:scenecut=0',
                       '-fps_mode', 'passthrough', '-f', 'h264', 'pipe:1']
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.threads = [threading.Thread(target=self._feed, daemon=True),
                        threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._errors, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _errors(self):
        for line in iter(self.process.stderr.readline, b''):
            self.errors.append(line.decode(errors='replace')[-1000:])

    def _enqueue(self, item, timeout):
        if timeout is not None:
            self.input.put(item, timeout=timeout)
            return
        # The offline supervisor may SIGSTOP the whole group for hours while
        # the operator records. A wall-clock queue deadline would falsely fail
        # immediately on resume. Still detect a genuinely failed child.
        while True:
            if self.failure or self.process.poll() is not None:
                raise RuntimeError(self.failure or '视频编码进程退出')
            try:
                self.input.put(item, timeout=.2)
                return
            except queue.Full:
                continue

    def submit(self, jpeg, metadata, *, timeout=0):
        if self.failure or self.closed:
            raise RuntimeError(self.failure or 'Encoder closed')
        if self.started is None:
            self.started = time.monotonic()
        try:
            self._enqueue((jpeg, metadata), timeout)
        except queue.Full:
            raise RuntimeError('视频编码跟不上采集，已停止本回合并保留已写入数据')
        self.accepted += 1

    def _feed(self):
        try:
            while True:
                item = self.input.get()
                if item is None:
                    break
                jpeg, metadata = item
                self.pending.put(metadata, timeout=5)
                data = struct.pack('<I', len(jpeg))+jpeg if self.native else jpeg
                view = memoryview(data)
                while view:
                    n = os.write(self.process.stdin.fileno(), view)
                    if n <= 0:
                        raise RuntimeError('Encoder input closed')
                    view = view[n:]
            self.process.stdin.close()
        except Exception as exc:
            self.failure = str(exc)
            self.process.kill()

    def _exact(self, size):
        result = bytearray()
        while len(result) < size:
            data = os.read(self.process.stdout.fileno(), size-len(result))
            if not data:
                if result:
                    raise RuntimeError('Truncated hardware encoder output')
                return b''
            result.extend(data)
        return bytes(result)

    def _packets(self):
        if self.native:
            while header := self._exact(4):
                size = struct.unpack('<I', header)[0]
                if not 0 < size <= 16*1024*1024:
                    raise RuntimeError('Invalid hardware encoder packet size')
                packet = self._exact(size)
                if len(packet) != size:
                    raise RuntimeError('Missing hardware encoder packet')
                yield packet
            return
        # x264 emits one AUD at each access-unit boundary. Keep only one frame
        # in memory; the final frame is flushed when stdin closes.
        import re
        marker = re.compile(b'\x00\x00(?:\x00)?\x01\x09')
        buffer = b''
        while chunk := os.read(self.process.stdout.fileno(), 65536):
            buffer += chunk
            while True:
                first = marker.search(buffer)
                second = marker.search(buffer, first.end()) if first else None
                if second is None:
                    break
                yield buffer[:second.start()]
                buffer = buffer[second.start():]
            if len(buffer) > 16*1024*1024:
                raise RuntimeError('Encoder produced an oversized access unit')
        if buffer:
            yield buffer

    def _read(self):
        try:
            for packet in self._packets():
                validate_packet(packet, first=self.count == 0)
                metadata = self.pending.get(timeout=5)
                self.callback(packet, metadata)
                self.count += 1
                self.last_output = time.monotonic()
        except Exception as exc:
            self.failure = str(exc)
            self.process.kill()

    def check(self):
        if self.failure:
            raise RuntimeError(self.failure)
        if self.process.poll() is not None:
            raise RuntimeError('视频编码进程退出: '+''.join(self.errors)[-1200:])
        if self.started and self.accepted-self.count > 3:
            if time.monotonic()-max(self.started, self.last_output) > 1:
                raise RuntimeError('视频编码超过 1 秒没有输出，保留当前回合')

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._enqueue(None, None if self.managed else 2)
            self.threads[0].join(None if self.managed else 8)
            self.threads[1].join(None if self.managed else 8)
            if self.threads[0].is_alive() or self.threads[1].is_alive():
                raise RuntimeError('视频编码结束超时，未标记为完整回合')
            code = self.process.wait(timeout=None if self.managed else 2)
            if code or self.failure or self.count != self.accepted:
                raise RuntimeError(self.failure or f'视频帧不完整: {self.count}/{self.accepted}, {code}')
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if not pipe.closed:
                    pipe.close()
