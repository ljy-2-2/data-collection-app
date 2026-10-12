import json
import hashlib
import pytest
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control" / "src"))

from teleop_control.umi_uploader import UmiRosbagUploader  # noqa: E402
from teleop_control.platform_auth import PlatformBearerAuth, PlatformTokenStore  # noqa: E402


@pytest.mark.parametrize("compact", [False, True])
def test_completed_umi_round_uploads_bundle_and_writes_receipt(tmp_path, compact):
    episode_dir = tmp_path / "rosbags" / "task" / "round-001"
    episode_dir.mkdir(parents=True)
    (episode_dir / "round-001_0.mcap").write_bytes(b"\x89MCAP0\r\nrecorded-data\x89MCAP0\r\n")
    (episode_dir / "meta.json").write_text(
        json.dumps({"author": "collector", "task_name": "UMI test", "episode": {"start_time": 10, "end_time": 12}}),
        encoding="utf-8",
    )
    (episode_dir / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  relative_file_paths: [round-001_0.mcap]\n", encoding="utf-8")
    if compact:
        media = episode_dir / "media"
        media.mkdir()
        for name in ("round-001_0.mcap", "metadata.yaml"):
            (episode_dir / name).rename(media / name)
        cameras = []
        for side in ("left", "right", "center"):
            data = (side + "-video").encode()
            (media / f"{side}.mp4").write_bytes(data)
            cameras.append({"topic": f"/{side}_camera/image_raw/compressed", "file": f"{side}.mp4",
                "sha256": hashlib.sha256(data).hexdigest(), "frame_count": 1, "frames": [{"log_time": 10}]})
        (media / "video-manifest.json").write_text(json.dumps({"format": "umi-mp4-v1", "state": "complete",
            "signals": {"file": "round-001_0.mcap", "sha256": hashlib.sha256((media / "round-001_0.mcap").read_bytes()).hexdigest()},
            "cameras": cameras}))
    uploaded = {}
    registered = []
    store = PlatformTokenStore(tmp_path / "test-auth.json")
    store.write(token="test-platform-token", expires_in=3600, user={"username": "collector"})

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "platform.test":
            assert request.headers.get("Authorization") == "Bearer test-platform-token"
        else:
            # MinIO rejects Bearer + presigned URL authentication together.
            assert "Authorization" not in request.headers
        if request.url.path == "/uploads/presign":
            names = json.loads(request.content)["filenames"]
            return httpx.Response(200, json={"urls": [
                {"filename": name, "put_url": f"https://upload.test/{name}", "key": f"raw/{name}"}
                for name in names
            ]})
        if request.url.host == "upload.test":
            uploaded[request.url.path.lstrip("/")] = request.content
            return httpx.Response(200)
        if request.url.path == "/episodes" and request.method == "POST":
            registered.append(json.loads(request.content))
            return httpx.Response(201, json={"id": registered[-1]["id"]})
        if request.url.path.startswith("/episodes/import-batches/"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handle), auth=PlatformBearerAuth(store)) as client:
        uploader = UmiRosbagUploader(tmp_path, "https://platform.test", client=client)
        uploader.scan_once()
        uploader.scan_once()

    expected = {"round-001_0.mcap", "meta.json", "metadata.yaml"}
    if compact:
        expected.update({"video-manifest.json", "left.mp4", "right.mp4", "center.mp4"})
        assert registered[0]["storage_keys"]["source_videos"] == ["raw/left.mp4", "raw/right.mp4", "raw/center.mp4"]
        assert registered[0]["storage_keys"]["video_manifest"] == "raw/video-manifest.json"
    assert set(uploaded) == expected
    assert uploaded["round-001_0.mcap"] == b"\x89MCAP0\r\nrecorded-data\x89MCAP0\r\n"
    assert registered[0]["source"] == "rosbag"
    assert registered[0]["task"] == "UMI test"
    assert registered[0]["duration_s"] == 2.0
    assert (episode_dir / ".data-platform-uploaded.json").is_file()

    assert len(registered) == 1
    assert registered[0]["format"] == "rosbag"
    assert registered[0]["storage_keys"]["mcap"].endswith("round-001_0.mcap")
    assert registered[0]["storage_keys"]["metadata"].endswith("metadata.yaml")
    assert "hdf5" not in registered[0]["storage_keys"]


def test_incomplete_or_split_bag_never_uploads(tmp_path):
    episode_dir = tmp_path / 'rosbags/task/round'
    episode_dir.mkdir(parents=True)
    (episode_dir / 'meta.json').write_text('{}')
    (episode_dir / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [round.mcap]\n')
    mcap = episode_dir / 'round.mcap'
    mcap.write_bytes(b'\x89MCAP0\r\nunfinished payload')
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))) as client:
        uploader = UmiRosbagUploader(tmp_path, 'https://platform.test', client=client)
        uploader.scan_once()
        assert not any(r.url.path == '/uploads/presign' for r in requests)
        assert not (episode_dir / '.data-platform-uploaded.json').exists()
        (episode_dir / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [round.mcap, next.mcap]\n')
        uploader.scan_once()
        assert not any(r.url.path == '/uploads/presign' for r in requests)


def test_interrupted_upload_retries_and_registered_conflict_is_idempotent(tmp_path):
    episode_dir = tmp_path / 'rosbags/task/round'
    episode_dir.mkdir(parents=True)
    (episode_dir / 'meta.json').write_text('{}')
    (episode_dir / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [round.mcap]\n')
    (episode_dir / 'round.mcap').write_bytes(b'\x89MCAP0\r\npayload\x89MCAP0\r\n')
    attempts = []
    registered = False

    def handle(request):
        if request.url.path == '/uploads/presign':
            attempts.append(json.loads(request.content)['episode_id'])
            if not registered:
                raise httpx.ConnectError('temporarily down')
            return httpx.Response(409)
        if request.url.path.startswith('/episodes/import-batches/'):
            return httpx.Response(200)
        if request.method == 'GET' and request.url.path.startswith('/episodes/'):
            return httpx.Response(200, json={'id': attempts[-1]})
        raise AssertionError(f'unexpected request: {request.url}')

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        uploader = UmiRosbagUploader(tmp_path, 'https://platform.test', client=client)
        uploader.scan_once()
        assert not (episode_dir / '.data-platform-uploaded.json').exists()
        registered = True
        uploader.scan_once()
        uploader.scan_once()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert (episode_dir / '.data-platform-uploaded.json').exists()
