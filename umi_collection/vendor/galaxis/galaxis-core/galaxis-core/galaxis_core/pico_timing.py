"""Observe source/receive clocks without rewriting samples or stopping a round."""
from collections import deque


class PicoTiming:
    def __init__(self, window_s=2.0, tolerance_s=0.1):
        self.window_s = window_s
        self.tolerance_ns = round(tolerance_s * 1e9)
        self.samples = deque()
        self.fault = None
        self.fault_at = float('-inf')

    def push(self, now, device, mono, seq, device_id):
        if min(device, mono) <= 0 or not device_id:
            self.fault, self.fault_at = 'PICO 原始时间或设备标识无效', now
            return
        if self.samples:
            _, old_device, old_mono, old_seq, old_id = self.samples[-1]
            if device <= old_device or mono <= old_mono or seq <= old_seq or device_id != old_id:
                self.samples.clear()
                self.fault, self.fault_at = 'PICO 时间/序号倒退或设备重连，等待稳定', now
        self.samples.append((now, device, mono, seq, device_id))
        while self.samples and self.samples[0][0] < now - self.window_s:
            self.samples.popleft()

    def failure(self, now):
        if self.fault and now - self.fault_at < self.window_s:
            return self.fault
        rows = self.samples
        if len(rows) < 2 or now - rows[-1][0] > 1.0:
            return '等待 PICO 连续实时数据'
        # Callback frequency can be high while old TCP packets are draining.
        # Compare original device time with bridge receive monotonic time.
        if rows[-1][2] - rows[0][2] < 1_000_000_000:
            return '正在验证 PICO 时序（约 1 秒）'
        offsets = [d - m for _, d, m, _, _ in rows]
        spread = max(offsets) - min(offsets)
        if spread > self.tolerance_ns:
            return f'PICO 数据积压或时钟不稳定（偏移变化 {spread / 1e6:.0f} ms），等待恢复后开始'
        return None
