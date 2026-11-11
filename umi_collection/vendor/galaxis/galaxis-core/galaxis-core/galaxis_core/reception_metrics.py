"""Bounded, passive per-topic recording diagnostics; never rewrite timestamps.

DDS timestamps and time.time_ns share the host wall clock. Negative deltas are
reported as clock anomalies, not interpreted as negative transport latency.
Counters include every recorded topic and allocate no per-frame history.
"""
import struct
import math
from collections.abc import Mapping


def message_info_value(info, key):
    """Jazzy raw subscriptions supply a dict; typed subscriptions use attributes."""
    value = info.get(key, 0) if isinstance(info, Mapping) else getattr(info, key, 0)
    return max(0, int(value or 0))


class DelayHistogram:
    # 1 ms buckets, bounded memory regardless of episode length.
    def __init__(self):
        self.bins = [0] * 201
        self.count = self.total = self.maximum = self.negative = 0
        self.minimum = None

    def add(self, ns):
        if ns < 0:
            self.negative += 1
            return
        self.count += 1
        self.total += ns
        self.maximum = max(self.maximum, ns)
        self.minimum = ns if self.minimum is None else min(self.minimum, ns)
        self.bins[min(ns // 1_000_000, 200)] += 1

    def snapshot(self):
        def percentile(q):
            rank = math.ceil(self.count * q)
            n = 0
            for i, count in enumerate(self.bins):
                n += count
                if n >= rank:
                    return min((i+1)*1_000_000, self.maximum) if i < 200 else self.maximum
        return dict(samples=self.count, negative_deltas=self.negative,
                    min_ns=self.minimum, max_ns=self.maximum,
                    mean_ns=self.total/self.count if self.count else None,
                    p50_upper_ns=percentile(.5) if self.count else None,
                    p95_upper_ns=percentile(.95) if self.count else None,
                    bucket_width_ns=1_000_000,
                    overflow_above_ns=200_000_000,
                    overflow_samples=self.bins[200])


def pico_device_stamp(payload):
    try:
        if len(payload) < 16 or payload[:2] not in (b'\x00\x01', b'\x00\x00'):
            return None
        order = '<' if payload[1] else '>'
        size = struct.unpack_from(order+'I', payload, 12)[0]
        if size < 1 or 16+size > len(payload):
            return None
        offset = 4 + ((16+size-4+7)//8)*8
        value = struct.unpack_from(order+'q', payload, offset)[0]
        return value if value > 0 else None
    except (struct.error, TypeError, IndexError):
        return None


class ReceptionMetrics:
    def __init__(self):
        self.topics = {}
        self.previous = {}
        self.histograms = {}
        self.pico = {}

    def received(self, topic, payload, arrival_ns, source_ns, callback_ns):
        row = self.topics.setdefault(topic, dict(
            samples=0, header_samples=0, first_header_ns=None, last_header_ns=None,
            first_arrival_ns=None, last_arrival_ns=None,
            header_gap_max_ns=0, header_gaps_over_200ms=0,
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
        def observe(name, delta):
            key = (topic, name)
            histogram = self.histograms.get(key)
            if histogram is None:
                histogram = self.histograms[key] = DelayHistogram()
            histogram.add(delta)
        if stamp is not None and arrival_ns > 0:
            observe('header_to_dds', arrival_ns-stamp)
        if topic.startswith('/pico/') and topic.endswith('/state'):
            device = pico_device_stamp(payload)
            if device is not None:
                p = self.pico.setdefault(topic, dict(samples=0, device_first_ns=device,
                    device_last_ns=device, device_gap_max_ns=0, device_nonincreasing=0,
                    host_burst_intervals=0, host_minus_device_min_ns=None,
                    host_minus_device_max_ns=None, previous_host_ns=arrival_ns))
                if p['samples']:
                    gap = device-p['device_last_ns']
                    p['device_gap_max_ns'] = max(p['device_gap_max_ns'], gap)
                    p['device_nonincreasing'] += int(gap <= 0)
                    host_gap = arrival_ns-p['previous_host_ns']
                    p['host_burst_intervals'] += int(0 <= host_gap < 1_000_000 and gap > 1_000_000)
                p['samples'] += 1
                p['device_last_ns'] = device
                p['previous_host_ns'] = arrival_ns
                delta = arrival_ns-device
                for key, fn in [('host_minus_device_min_ns',min),('host_minus_device_max_ns',max)]:
                    p[key] = delta if p[key] is None else fn(p[key],delta)
        prior = self.previous.get(topic)
        if prior:
            row['arrival_gap_max_ns'] = max(row['arrival_gap_max_ns'], arrival_ns-prior[0])
            if stamp is not None and prior[1] is not None:
                gap = stamp-prior[1]
                row['header_gap_max_ns'] = max(row['header_gap_max_ns'], gap)
                row['header_gaps_over_200ms'] += int(gap > 200_000_000)
                row['header_nonincreasing'] += int(gap <= 0)
        if source_ns > 0 and arrival_ns > 0:
            observe('publisher_to_dds', arrival_ns-source_ns)
            row['publisher_to_dds_max_ns'] = max(row['publisher_to_dds_max_ns'], arrival_ns-source_ns)
            row['clock_anomalies'] += int(arrival_ns < source_ns)
        if arrival_ns > 0:
            delay = callback_ns-arrival_ns
            observe('dds_to_callback', delay)
            row['dds_to_callback_max_ns'] = max(row['dds_to_callback_max_ns'], delay)
            row['dds_to_callback_over_200ms'] += int(delay > 200_000_000)
            row['clock_anomalies'] += int(delay < 0)
        if row['first_arrival_ns'] is None:row['first_arrival_ns'] = arrival_ns
        row['last_arrival_ns'] = arrival_ns
        if stamp is not None:
            if row['first_header_ns'] is None:row['first_header_ns'] = stamp
            row['last_header_ns'] = stamp
            row['header_samples'] += 1
        row['samples'] += 1
        self.previous[topic] = (arrival_ns, stamp)

    def written(self, topic, elapsed_ns):
        row = self.topics[topic]
        key = (topic, 'write_call')
        histogram = self.histograms.get(key)
        if histogram is None:
            histogram = self.histograms[key] = DelayHistogram()
        histogram.add(elapsed_ns)
        row['write_max_ns'] = max(row['write_max_ns'], elapsed_ns)
        row['writes_over_200ms'] += int(elapsed_ns > 200_000_000)

    def snapshot(self):
        result = dict(version=1, warning_threshold_ms=200,
                    timing_basis='host_wall_clock_dds_and_monotonic_write_duration',
                    topics={topic: dict(row) for topic, row in self.topics.items()})
        for row in result['topics'].values():
            for prefix,count,first,last in (('source',row['header_samples'],row['first_header_ns'],row['last_header_ns']),
                                           ('arrival',row['samples'],row['first_arrival_ns'],row['last_arrival_ns'])):
                row[prefix+'_hz'] = ((count-1)*1e9/(last-first)
                    if count>1 and first is not None and last is not None and last>first else None)
        result['latency_definitions'] = {
            'header_to_dds': 'host timestamp to recorder DDS arrival; PICO header is host reception, not headset sampling',
            'publisher_to_dds': 'DDS publisher source timestamp to recorder DDS arrival',
            'dds_to_callback': 'DDS arrival to subscriber callback',
            'write_call': 'MCAP writer call duration; not durable disk fsync latency',
            'pico_clock_delta': 'host-device clock difference includes unsynchronized clock offset; NOT absolute network latency'}
        for (topic, name), histogram in self.histograms.items():
            result['topics'][topic].setdefault('latency', {})[name] = histogram.snapshot()
        for topic, row in self.pico.items():
            p = dict(row)
            p.pop('previous_host_ns')
            span = p['device_last_ns']-p['device_first_ns']
            p['device_hz'] = (p['samples']-1)*1e9/span if span > 0 else None
            result['topics'][topic]['pico_timing'] = p
        if hasattr(self, 'writer_queue'):
            result['writer_queue'] = dict(self.writer_queue)
        return result
