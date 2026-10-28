"""Confirm required topics in the growing MCAP, before starting episode time."""

from io import BytesIO
from pathlib import Path
import struct
import time


READY_TIMEOUT_S = 8.0
START_SERVICE_TIMEOUT_S = 25.0
MAGIC = b"\x89MCAP0\r\n"
MAX_RECORD_BYTES = 64 * 1024 * 1024


def required_recording_topics(record_topics):
    """Only enabled continuous UMI streams; optional/latched topics cannot block."""
    required = {
        topic for topic in record_topics
        if topic.endswith(("/image_raw/compressed", "/image_raw"))
        or topic in {
            "/pico/hmd/state", "/pico/controller/left/state",
            "/pico/controller/right/state", "/galaxis/gripper/left/joint_state",
            "/galaxis/gripper/right/joint_state",
        }
    }
    # Non-UMI callers must still demonstrate a message in their requested set.
    return required


class McapReadiness:
    """Incrementally inspect complete records; never read a partially flushed chunk.

    A file/header/subscription announcement alone is not recording readiness.
    Message records must exist for every required topic. The recording format,
    compression, ROS subscriptions and timestamps are not changed by this reader.
    """

    def __init__(self, directory, required_topics, record_topics):
        # Import before starting the child, so missing dependencies fail cleanly.
        from mcap.data_stream import ReadDataStream
        from mcap.records import Channel, Chunk, Message
        from mcap.stream_reader import breakup_chunk

        self._stream = ReadDataStream
        self._channel = Channel
        self._chunk = Chunk
        self._message = Message
        self._breakup_chunk = breakup_chunk
        self.directory = Path(directory)
        self.required = set(required_topics)
        self.requested = set(record_topics)
        self.seen = set()
        self._files = {}

    @property
    def ready(self):
        return self.required <= self.seen if self.required else bool(self.requested & self.seen)

    @property
    def missing(self):
        return sorted(self.required - self.seen) if self.required else sorted(self.requested)

    def scan(self):
        for path in sorted([*self.directory.glob("*.mcap"), *self.directory.glob('*.mcap.partial')]):
            offset, channels = self._files.get(path, (0, {}))
            with path.open("rb") as source:
                size = path.stat().st_size
                if not offset:
                    if size < len(MAGIC):
                        continue
                    if source.read(len(MAGIC)) != MAGIC:
                        raise ValueError(f"Invalid MCAP header: {path.name}")
                    offset = len(MAGIC)
                while offset + 9 <= size:
                    source.seek(offset)
                    header = source.read(9)
                    opcode, length = struct.unpack("<BQ", header)
                    if length > MAX_RECORD_BYTES:
                        raise ValueError(f"MCAP readiness record exceeds {MAX_RECORD_BYTES} bytes")
                    if offset + 9 + length > size:
                        break  # Writer has not finished this record yet.
                    if opcode in (0x04, 0x05, 0x06):
                        payload = self._stream(BytesIO(source.read(length)))
                        if opcode == 0x04:
                            records = [self._channel.read(payload)]
                        elif opcode == 0x05:
                            records = [self._message.read(payload, length)]
                        else:
                            records = self._breakup_chunk(self._chunk.read(payload), validate_crc=True)
                        for record in records:
                            if isinstance(record, self._channel):
                                channels[record.id] = record.topic
                            elif isinstance(record, self._message):
                                topic = channels.get(record.channel_id)
                                if topic:
                                    self.seen.add(topic)
                    offset += 9 + length
                    self._files[path] = (offset, channels)
                    if self.ready:
                        return
                self._files[path] = (offset, channels)


def wait_for_recording_ready(process, probe, *, timeout=READY_TIMEOUT_S,
                             keep_running=lambda: True, clock=time.monotonic,
                             sleep=time.sleep):
    deadline = clock() + timeout
    while keep_running():
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"录制器在就绪前退出，退出码 {code}")
        probe.scan()
        if probe.ready:
            if process.poll() is not None:
                raise RuntimeError("录制器在就绪前退出")
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("等待录制数据超时，未录到必要通道: " + ", ".join(probe.missing))
        sleep(min(0.05, remaining))
    raise RuntimeError("录制启动被关闭流程中断")
