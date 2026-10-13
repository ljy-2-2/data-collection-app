"""Camera recording contract checks, independent of ROS and image decoders."""
from collections import deque
from pathlib import Path


def load_contract(path=Path('/etc/galaxis/collection_profile.yaml')):
    import yaml
    if not path.exists():
        return {}
    profile = yaml.safe_load(path.read_text())
    if not isinstance(profile, dict) or not profile.get('camera_quality'):
        return {}
    limits = profile['camera_quality']
    if limits.get('version', 1) < 2:
        limits = dict(version=2, min_rate_hz=27.0, rate_window_sec=5.0,
                                    warn_frame_gap_sec=0.5, stop_min_rate_hz=20.0,
                                    stop_low_rate_sec=10.0, max_frame_gap_sec=3.0, warmup_sec=1.0)
    return dict(cameras={role: profile['cameras'][role] for role in profile['active_cameras']},
                limits=limits)


def timing_failure(times, *, min_rate=29.7, max_gap=.075, window=3.0):
    """Check actual timestamps, including local slow windows; never invent frames."""
    if len(times) < 2:
        return '图像样本不足，无法验证采集帧率'
    start = 0
    for i in range(1, len(times)):
        gap = times[i] - times[i-1]
        if gap <= 0:
            return '图像时间戳重复或倒退'
        if gap > max_gap:
            return f'图像帧间隔 {gap*1000:.1f} ms 超过 {max_gap*1000:g} ms'
        while start < i-1 and times[i]-times[start+1] >= window:
            start += 1
        span = times[i]-times[start]
        if span >= window and (i-start)/span < min_rate:
            return f'图像实际帧率 {(i-start)/span:.2f} Hz 低于 {min_rate:g} Hz'
    span = times[-1]-times[0]
    rate = (len(times)-1)/span
    if rate < min_rate:
        return f'图像平均帧率 {rate:.2f} Hz 低于 {min_rate:g} Hz'
    return ''


class CameraCadence:
    """Require a healthy window; retain a detected fault through gate polling."""
    def __init__(self, minimum=29.0, max_gap=.2, window=3.0, hold_low_sec=0.0):
        self.minimum, self.max_gap, self.window = minimum, max_gap, window
        self.hold_low_sec = hold_low_sec
        self.low_since = None
        self.samples = deque()
        self.fault = None

    def push(self, arrival, source_ns):
        if self.samples:
            previous_arrival, previous_source = self.samples[-1]
            gap = (source_ns-previous_source)/1e9
            if gap <= 0 or gap > self.max_gap or arrival-previous_arrival > self.max_gap:
                self.fault = (arrival, '相机时间戳异常或帧间隔超限')
        self.samples.append((arrival, source_ns))
        while len(self.samples)>2 and arrival-self.samples[1][0] > self.window:
            self.samples.popleft()

    def failure(self, now):
        if self.fault and now-self.fault[0] <= self.window:
            return self.fault[1]
        if len(self.samples)<2 or now-self.samples[-1][0] > self.max_gap:
            return '相机尚未持续出帧或图像已停顿'
        if self.samples[-1][0]-self.samples[0][0] < self.window-.05:
            return '正在验证三路相机持续 30 帧采集能力'
        arrival = [x[0] for x in self.samples]
        source = [(x[1]-self.samples[0][1])/1e9 for x in self.samples]
        problem = (timing_failure(arrival, min_rate=self.minimum, max_gap=self.max_gap, window=self.window)
                   or timing_failure(source, min_rate=self.minimum, max_gap=self.max_gap, window=self.window))
        if problem and '帧率' in problem and self.hold_low_sec:
            if self.low_since is None:
                self.low_since = now
            return problem if now-self.low_since >= self.hold_low_sec else ''
        self.low_since = None
        return problem


def recording_timing_result(arrival_ns, source_ns, limits):
    """Replay the live sustained-failure policy; mild jitter is a warning."""
    if len(arrival_ns) < 2 or len(arrival_ns) != len(source_ns):
        return '图像时间记录不完整', []
    arrival = [(t-arrival_ns[0])/1e9 for t in arrival_ns]
    source = [(t-source_ns[0])/1e9 for t in source_ns]
    maximum = limits['max_frame_gap_sec']
    for values in (arrival, source):
        hard = timing_failure(values, min_rate=0, max_gap=maximum)
        if hard:
            return hard, []
    guard = CameraCadence(limits['stop_min_rate_hz'], maximum,
                          limits['rate_window_sec'], limits['stop_low_rate_sec'])
    for at, stamp in zip(arrival, source_ns):
        guard.push(at, stamp)
        issue = guard.failure(at)
        if issue and not issue.startswith('正在验证') and len(guard.samples) >= 2:
            return issue, []
    warnings = []
    for label, values in [('采样',source),('入包',arrival)]:
        issue = timing_failure(values, min_rate=limits['min_rate_hz'],
                               max_gap=limits['warn_frame_gap_sec'], window=limits['rate_window_sec'])
        if issue:
            warnings.append(label+'：'+issue)
    return '', warnings
