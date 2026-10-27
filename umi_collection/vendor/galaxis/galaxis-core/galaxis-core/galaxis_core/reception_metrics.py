"""Bounded, passive per-topic recording diagnostics; never rewrite timestamps.

DDS timestamps and time.time_ns share the host wall clock. Negative deltas are
reported as clock anomalies, not interpreted as negative transport latency.
Counters include every recorded topic and allocate no per-frame history.
"""
import struct
from collections.abc import Mapping


def message_info_value(info, key):
    """Jazzy raw subscriptions supply a dict; typed subscriptions use attributes."""
    value = info.get(key, 0) if isinstance(info, Mapping) else getattr(info, key, 0)
    return max(0, int(value or 0))


class ReceptionMetrics:
    def __init__(self):
        self.topics = {}
        self.previous = {}

    def received(self, topic, payload, arrival_ns, source_ns, callback_ns):
        row = self.topics.setdefault(topic, dict(
            samples=0, header_gap_max_ns=0, header_gaps_over_200ms=0,
            header_nonincreasing=0, arrival_gap_max_ns=0,
            publisher_to_dds_max_ns=0, dds_to_callback_max_ns=0,
            dds_to_callback_over_200ms=0, write_max_ns=0,
            writes_over_200ms=0, clock_anomalies=0))
        stamp = None
        # Only known Header-first / Time-first sensor messages. Other topics
        # still get arrival/queue/write metrics without guessing their schema.
        stamped = (topic in ('/joint_states', '/galaxis/gripper/opening_state')
                   or topic.startswith('/pico/') and topic.endswith('/state')
                   or topic.endswith(('/image_raw', '/image_raw/compressed', '/joint_state')))
        if stamped and len(payload) >= 12 and payload[:2] in (b'\x00\x01', b'\x00\x00'):
            sec, nano = struct.unpack_from(('<' if payload[1] else '>')+'iI', payload, 4)
            if 0 <= nano < 1_000_000_000:
                stamp = sec * 1_000_000_000 + nano
        prior = self.previous.get(topic)
        if prior:
            row['arrival_gap_max_ns'] = max(row['arrival_gap_max_ns'], arrival_ns-prior[0])
            if stamp is not None and prior[1] is not None:
                gap = stamp-prior[1]
                row['header_gap_max_ns'] = max(row['header_gap_max_ns'], gap)
                row['header_gaps_over_200ms'] += int(gap > 200_000_000)
                row['header_nonincreasing'] += int(gap <= 0)
        if source_ns > 0 and arrival_ns > 0:
            row['publisher_to_dds_max_ns'] = max(row['publisher_to_dds_max_ns'], arrival_ns-source_ns)
            row['clock_anomalies'] += int(arrival_ns < source_ns)
        if arrival_ns > 0:
            delay = callback_ns-arrival_ns
            row['dds_to_callback_max_ns'] = max(row['dds_to_callback_max_ns'], delay)
            row['dds_to_callback_over_200ms'] += int(delay > 200_000_000)
            row['clock_anomalies'] += int(delay < 0)
        row['samples'] += 1
        self.previous[topic] = (arrival_ns, stamp)

    def written(self, topic, elapsed_ns):
        row = self.topics[topic]
        row['write_max_ns'] = max(row['write_max_ns'], elapsed_ns)
        row['writes_over_200ms'] += int(elapsed_ns > 200_000_000)

    def snapshot(self):
        result = dict(version=1, warning_threshold_ms=200,
                    timing_basis='host_wall_clock_dds_and_monotonic_write_duration',
                    topics={topic: dict(row) for topic, row in self.topics.items()})
        if hasattr(self, 'writer_queue'):
            result['writer_queue'] = dict(self.writer_queue)
        return result
