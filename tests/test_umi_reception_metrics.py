import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
    'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'))
from galaxis_core.reception_metrics import ReceptionMetrics


def header(ns):
    sec, nano = divmod(ns, 10**9)
    return b'\x00\x01\x00\x00' + struct.pack('<iI', sec, nano)


def test_distinguishes_source_gap_from_callback_and_write_delays_for_all_sensors():
    metrics = ReceptionMetrics()
    for topic in ('/left_camera/image_raw/compressed', '/center_camera/image_raw/compressed',
                  '/right_camera/image_raw/compressed', '/galaxis/gripper/left/joint_state',
                  '/galaxis/gripper/right/joint_state', '/pico/hmd/state',
                  '/pico/controller/left/state', '/pico/controller/right/state'):
        metrics.received(topic, header(10**9), 10**9, 10**9, 10**9)
        metrics.received(topic, header(1_010_000_000), 1_010_000_000, 1_010_000_000, 1_260_000_000)
        metrics.written(topic, 300_000_000)
        row = metrics.snapshot()['topics'][topic]
        assert row['header_gap_max_ns'] == 10_000_000
        assert row['header_gaps_over_200ms'] == 0
        assert row['dds_to_callback_over_200ms'] == 1
        assert row['writes_over_200ms'] == 1
        metrics.received(topic, header(1_310_000_000), 1_310_000_000, 1_310_000_000, 1_310_000_000)
        assert metrics.topics[topic]['header_gaps_over_200ms'] == 1


def test_clock_anomalies_and_unstamped_messages_are_not_faked_into_latency():
    metrics = ReceptionMetrics()
    metrics.received('/unknown', header(10**9), 100, 200, 50)
    row = metrics.topics['/unknown']
    assert row['clock_anomalies'] == 2
    assert row['publisher_to_dds_max_ns'] == row['dds_to_callback_max_ns'] == 0
    assert metrics.previous['/unknown'][1] is None


def test_statistics_are_bounded_and_snapshot_is_independent():
    metrics = ReceptionMetrics()
    for i in range(10000):
        metrics.received('/joint_states', header(i*10_000_000), i*10_000_000,
                         i*10_000_000, i*10_000_000)
    copy = metrics.snapshot()
    copy['topics']['/joint_states']['samples'] = 0
    assert metrics.topics['/joint_states']['samples'] == 10000
    assert len(metrics.previous) == len(metrics.topics) == 1


def test_raw_ros_message_info_dict_keeps_dds_clock_and_sequence():
    from galaxis_core.reception_metrics import message_info_value
    from types import SimpleNamespace
    values=dict(received_timestamp=123456789,source_timestamp=123456000,publication_sequence_number=42)
    for info in (values,SimpleNamespace(**values)):
        assert [message_info_value(info,k) for k in values]==list(values.values())
    assert message_info_value({},'received_timestamp')==0
    assert message_info_value({'publication_sequence_number':-1},'publication_sequence_number')==0
