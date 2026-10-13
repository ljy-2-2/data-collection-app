"""Bounded histories of received motor frames, separate from cached status."""

from collections import deque


def observed_hz(stamps):
    if len(stamps) < 2 or stamps[-1] <= stamps[0]:
        return None
    return (len(stamps) - 1) / (stamps[-1] - stamps[0])


class FeedbackBuffer:
    def __init__(self, sides, capacity=512):
        self.rows = {side: deque(maxlen=capacity) for side in sides}
        self.counts = dict.fromkeys(sides, 0)
        self.dropped = dict.fromkeys(sides, 0)
        self.stamps = {side: deque(maxlen=101) for side in sides}

    def record(self, side, feedback):
        self.counts[side] += 1
        feedback.sequence = self.counts[side]
        rows = self.rows[side]
        if len(rows) == rows.maxlen:
            self.dropped[side] += 1
        # Copy now: later status updates or simulation commands must not alter
        # the measurement which was received at this timestamp.
        rows.append(feedback.status())
        self.stamps[side].append(feedback.received)

    def drain(self):
        result = {side: list(rows) for side, rows in self.rows.items()}
        for rows in self.rows.values():
            rows.clear()
        return result

    def timing(self, side):
        return {"received_count": self.counts[side],
                "actual_hz": observed_hz(self.stamps[side]),
                "queue_dropped": self.dropped[side]}
