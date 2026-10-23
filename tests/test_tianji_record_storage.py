"""Long recordings must not retain a growing graph that stalls the TLS reader."""
import gc
import json

import pytest

from FusionDataCollector.collector import RecordRows, attach_actions, attach_feedback
from fusion_camera.episode import match_bundles, stats


def test_record_rows_snapshot_index_sort_and_rewrite(tmp_path):
    path = tmp_path/'rows.jsonl'
    rows = RecordRows(path)
    try:
        source = {'timestamp_ns': 2, 'nested': {'values': [1, 2]}}
        rows.append(source)
        source['nested']['values'].append(3)
        rows.append({'timestamp_ns': 1})
        assert rows[0]['nested']['values'] == [1, 2]
        snapshot = rows[0]
        snapshot['nested']['values'].append(4)
        assert rows[0]['nested']['values'] == [1, 2]
        rows[0] = snapshot
        rows.sort(key=lambda row: row['timestamp_ns'])
        assert rows[-1]['nested']['values'] == [1, 2, 4]
        assert stats(rows)['actual_hz'] == 1e9
        for index in (-3, 2):
            with pytest.raises(IndexError):
                rows[index]
        rows.flush()
        assert all(json.loads(line) for line in path.read_text().splitlines())
    finally:
        rows.close()
    assert path.exists()  # Failed/incomplete finalization retains working evidence.


def test_record_rows_do_not_retain_sample_object_graphs(tmp_path):
    rows = RecordRows(tmp_path/'long.jsonl')
    try:
        gc.collect()
        baseline = len(gc.get_objects())
        for index in range(30_000):
            rows.append(dict(sequence=index, positions=list(range(14)),
                             camera_settings=dict(size=[1920, 1080])))
        gc.collect()
        assert len(gc.get_objects()) - baseline < 200
        assert len(rows) == 30_000
        assert rows[0]['sequence'] == 0 and rows[-1]['sequence'] == 29_999
        assert rows.offsets.itemsize * len(rows.offsets) <= 30_000 * 8
    finally:
        rows.close(remove=True)
    assert not rows.path.exists()


def test_disk_backed_alignment_matches_in_memory_results(tmp_path):
    opened = []
    def store(name, source):
        rows = RecordRows(tmp_path/(name+'.jsonl'))
        opened.append(rows)
        for row in source:
            rows.append(row)
        return rows
    joints = [dict(timestamp_ns=t, sequence=i, tick=i, frame_serial=i, pc_recv_ns=t+100,
                   position_deg=list(range(14)), velocity_deg_s=[0]*14, torque_nm=[1]*14,
                   joint_names=['joint'+str(j) for j in range(14)])
              for i, t in enumerate((10_000_000, 40_000_000, 70_000_000))]
    cameras = {'left_color': [dict(timestamp_ns=r['timestamp_ns'], frame_index=i)
                             for i, r in enumerate(joints)]}
    grippers = {'left': [dict(pc_feedback_received_monotonic_ns=r['timestamp_ns'],
                              motor_position_rad=.5, opening_angle_rad=.25) for r in joints],
                'right': []}
    actions = [dict(pc_monotonic_ns=r['timestamp_ns'], joints_deg={'left': [i]*7},
                    gripper_targets_rad={'left': .8},
                    gripper_target_sources={'left': {'source': 'last_controller_target'}})
               for i, r in enumerate(joints)]
    before = dict(boot_id='test', pc_midpoint_ns=0, offset_ns=0, uncertainty_ns=1)
    after = dict(before, pc_midpoint_ns=1_000_000_000)
    try:
        expected = match_bundles(joints, cameras)
        disk_grippers = {name: store(name, rows) for name, rows in grippers.items()}
        disk_actions = store('actions', actions)
        actual = match_bundles(store('joints', joints),
                               {name: store(name, rows) for name, rows in cameras.items()},
                               output=store('bundles', []))
        for bundles, feedback, commands in ((expected, grippers, actions),
                                             (actual, disk_grippers, disk_actions)):
            attach_feedback(bundles, feedback, before, after)
            attach_actions(commands, bundles, before, after, ('left',))
        assert list(actual) == expected
        assert list(disk_actions) == actions
        assert list(disk_grippers['left']) == grippers['left']
        assert stats(actual) == stats(expected)
    finally:
        for rows in opened:
            rows.close(remove=True)
