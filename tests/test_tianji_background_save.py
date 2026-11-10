"""Exercise durable handoff, independent finalization and publication failures."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_tianji_collection import account, test_ab_episode_and_platform_round_trip as round_trip
from teleop_control import tianji_local_queue as queue


@pytest.mark.parametrize('fault', [False, 'fault', 'queue'])
def test_independent_save_preserves_images_actions_annotations_and_upload(tmp_path, account, fault):
    round_trip(tmp_path, account, 'dual', 'mjpeg', None, False, fault,
               annotated=True, background=True)


def test_finalization_failure_retains_raw_and_retries_before_newer_jobs(tmp_path, monkeypatch):
    queue.configure(tmp_path, tmp_path / 'saved')
    first = tmp_path / 'pending/first'
    second = tmp_path / 'pending/second'
    for n, source in enumerate((first, second)):
        source.mkdir()
        (source / 'raw').write_bytes(b'original capture')
        queue.atomic_json(source / queue.REQUEST, dict(version=1, queued_at_ns=n,
            episode=source.name, info={'teleop_session_id': 'session'}))
    process = Mock(side_effect=OSError('disk full'))
    monkeypatch.setattr(queue, 'process_request', process)
    status = queue.scan_once(tmp_path)
    assert status['failed'] == 1 and status['pending'] == 1
    process.assert_called_once()
    assert (first / 'raw').read_bytes() == b'original capture'
    assert not list((tmp_path / 'saved').iterdir())
    queue.scan_once(tmp_path)
    process.assert_called_once()  # Backoff never lets later jobs overtake a failure.


def test_existing_output_is_never_overwritten(tmp_path):
    target = tmp_path / 'saved'
    queue.configure(tmp_path, target)
    source = tmp_path / 'pending/episode'
    source.mkdir()
    (target / 'episode').mkdir()
    (target / 'episode/user-data').write_bytes(b'keep')
    request = dict(version=1, episode='episode', byteorder=sys.byteorder,
                   job_id='test', destination=str(target))
    with pytest.raises(RuntimeError, match='目标目录已存在'):
        queue._process_request(tmp_path, source, request)
    assert (target / 'episode/user-data').read_bytes() == b'keep'
    assert source.exists()


def test_path_escape_and_symlink_rejected(tmp_path):
    with pytest.raises(ValueError): queue._relative(tmp_path, '../outside')
    (tmp_path / 'link').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError): queue._relative(tmp_path, 'link/escape')
