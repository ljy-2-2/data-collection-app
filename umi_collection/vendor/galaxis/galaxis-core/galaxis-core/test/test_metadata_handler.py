"""Unit tests for MetadataV2Handler.

ROS2, jsonschema, and airoa_metadata dependencies are mocked via ``conftest.mock_rclpy``.
"""

import json
import sys

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def handler(mock_rclpy):
    """Create a MetadataV2Handler node."""
    from galaxis_core.metadata_handler import MetadataV2Handler

    node = MetadataV2Handler()
    return node


def _make_string_trigger_request(message: str):
    """Create a fake StringTrigger.Request with the given message."""
    req = type("Request", (), {"message": message})()
    return req


def _make_string_trigger_response():
    """Create a fake StringTrigger.Response."""
    return type("Response", (), {"success": False, "message": ""})()


def _make_trigger_request():
    """Create a fake Trigger.Request."""
    return type("Request", (), {})()


def _make_trigger_response():
    """Create a fake Trigger.Response."""
    return type("Response", (), {"success": False, "message": ""})()


# ===================================================================
# TestInitializeMetadata
# ===================================================================


class TestInitializeMetadata:
    """Tests for metadata initialization."""

    def test_has_uuid(self, handler):
        assert handler.metadata.uuid
        assert isinstance(handler.metadata.uuid, str)
        assert len(handler.metadata.uuid) > 0

    def test_has_schema_version_2_0(self, handler):
        assert handler.metadata.schema_version == "2.0"

    def test_has_schema_uri_in_json(self, handler):
        metadata_json = json.loads(handler.metadata.to_json())
        assert metadata_json["$schema"].startswith("https://")

    def test_has_top_level_attributes(self, handler):
        for attr in [
            "robot",
            "environment",
            "runner",
            "devices",
            "programs",
            "files",
            "episode",
            "labels",
            "segments",
        ]:
            assert hasattr(handler.metadata, attr)

    def test_callback_resets_to_new_uuid(self, handler):
        old_uuid = handler.metadata.uuid
        req = _make_trigger_request()
        resp = _make_trigger_response()
        handler.initialize_metadata_callback(req, resp)
        assert handler.metadata.uuid != old_uuid
        assert resp.success is True


# ===================================================================
# TestValidateAndRespond
# ===================================================================


class TestValidateAndRespond:
    """Tests for ``validate_and_respond``."""

    def test_valid_json_passes(self, handler):
        resp = _make_string_trigger_response()
        result_dict, resp = handler.validate_and_respond(
            '{"key": "value"}',
            handler.schema_validator,
            "ok",
            "fail",
            resp,
        )
        assert resp.success is True
        assert result_dict == {"key": "value"}

    def test_invalid_json_fails(self, handler):
        resp = _make_string_trigger_response()
        result_dict, resp = handler.validate_and_respond(
            "not json {{",
            handler.schema_validator,
            "ok",
            "fail",
            resp,
        )
        assert resp.success is False
        assert "Invalid JSON" in resp.message

    def test_validation_error_fails(self, handler):
        # Patch the validator to raise a ValidationError
        jsonschema_exc = sys.modules["jsonschema.exceptions"]
        original_validate = handler.schema_validator.validate

        def raise_validation_error(instance):
            raise jsonschema_exc.ValidationError("bad field")

        handler.schema_validator.validate = raise_validation_error
        resp = _make_string_trigger_response()
        result_dict, resp = handler.validate_and_respond(
            '{"key": "value"}',
            handler.schema_validator,
            "ok",
            "fail",
            resp,
        )
        assert resp.success is False
        assert "bad field" in resp.message
        handler.schema_validator.validate = original_validate


# ===================================================================
# TestSetCallbacks
# ===================================================================


class TestSetCallbacks:
    """Tests for set_robot, set_environment, set_runner, set_devices."""

    def test_set_robot(self, handler):
        req = _make_string_trigger_request('{"type": "HSR2", "id": "robot-1"}')
        resp = _make_string_trigger_response()
        handler.set_robot_callback(req, resp)
        assert resp.success is True
        assert handler.metadata.robot.type == "HSR2"
        assert handler.metadata.robot.id == "robot-1"

    def test_set_robot_replaces(self, handler):
        req1 = _make_string_trigger_request('{"type": "A", "id": "1"}')
        resp1 = _make_string_trigger_response()
        handler.set_robot_callback(req1, resp1)

        req2 = _make_string_trigger_request('{"type": "B", "id": "2"}')
        resp2 = _make_string_trigger_response()
        handler.set_robot_callback(req2, resp2)
        assert handler.metadata.robot.type == "B"
        assert handler.metadata.robot.id == "2"

    def test_set_environment(self, handler):
        req = _make_string_trigger_request('{"type": "real_world", "site": "lab-1"}')
        resp = _make_string_trigger_response()
        handler.set_environment_callback(req, resp)
        assert resp.success is True
        assert handler.metadata.environment.type == "real_world"
        assert handler.metadata.environment.site == "lab-1"

    def test_set_runner(self, handler):
        req = _make_string_trigger_request(
            '{"type": "operator", "name": "user-1", "organization": "org"}'
        )
        resp = _make_string_trigger_response()
        handler.set_runner_callback(req, resp)
        assert resp.success is True
        assert handler.metadata.runner.name == "user-1"

    def test_set_devices(self, handler):
        devices = [{"role": "controller", "type": "gamepad", "id": "pad-1"}]
        req = _make_string_trigger_request(json.dumps(devices))
        resp = _make_string_trigger_response()
        handler.set_devices_callback(req, resp)
        assert resp.success is True
        assert len(handler.metadata.devices) == 1
        assert handler.metadata.devices[0].role == "controller"
        assert handler.metadata.devices[0].type == "gamepad"
        assert handler.metadata.devices[0].id == "pad-1"

    def test_set_episode(self, handler):
        episode = {
            "start_time": 1.0,
            "end_time": 10.0,
            "success": True,
            "label": "task-1",
        }
        req = _make_string_trigger_request(json.dumps(episode))
        resp = _make_string_trigger_response()
        handler.set_episode_callback(req, resp)
        assert resp.success is True
        assert handler.metadata.episode.start_time == 1.0
        assert handler.metadata.episode.end_time == 10.0
        assert handler.metadata.episode.success is True
        assert handler.metadata.episode.label == "task-1"


# ===================================================================
# TestAddCallbacks
# ===================================================================


class TestAddCallbacks:
    """Tests for add_file, add_program, extend_programs, add_label."""

    def test_add_file_appends(self, handler):
        req = _make_string_trigger_request('{"type": "mcap", "name": "data.mcap"}')
        resp = _make_string_trigger_response()
        handler.add_file_callback(req, resp)
        assert len(handler.metadata.files) == 1
        assert handler.metadata.files[0].name == "data.mcap"

    def test_add_program_appends(self, handler):
        program = {"role": "interface", "name": "teleop", "source": {}}
        req = _make_string_trigger_request(json.dumps(program))
        resp = _make_string_trigger_response()
        handler.add_program_callback(req, resp)
        assert len(handler.metadata.programs) == 1
        assert handler.metadata.programs[0].role == "interface"

    def test_extend_programs_appends_list(self, handler):
        programs = [
            {"role": "interface", "name": "teleop", "source": {}},
            {"role": "data_collection", "name": "recorder", "source": {}},
        ]
        req = _make_string_trigger_request(json.dumps(programs))
        resp = _make_string_trigger_response()
        handler.extend_programs_callback(req, resp)
        assert len(handler.metadata.programs) == 2

    def test_add_label_appends_string(self, handler):
        req = _make_string_trigger_request("pick up the cup")
        resp = _make_string_trigger_response()
        handler.add_label_callback(req, resp)
        assert resp.success is True
        assert handler.metadata.labels == ["pick up the cup"]

    def test_add_multiple_labels(self, handler):
        for label in ["step 1", "step 2", "step 3"]:
            req = _make_string_trigger_request(label)
            resp = _make_string_trigger_response()
            handler.add_label_callback(req, resp)
        assert handler.metadata.labels == ["step 1", "step 2", "step 3"]

    def test_add_empty_label_rejected(self, handler):
        req = _make_string_trigger_request("")
        resp = _make_string_trigger_response()
        handler.add_label_callback(req, resp)
        assert resp.success is False
        assert handler.metadata.labels == []

    def test_add_whitespace_label_rejected(self, handler):
        req = _make_string_trigger_request("   ")
        resp = _make_string_trigger_response()
        handler.add_label_callback(req, resp)
        assert resp.success is False
        assert handler.metadata.labels == []


# ===================================================================
# TestAddSegmentCallback
# ===================================================================


class TestAddSegmentCallback:
    """Tests for ``add_segment_callback``."""

    def test_segment_appends(self, handler):
        segment = {
            "start_time": 10.0,
            "end_time": 20.0,
            "label_idx": 0,
            "success": True,
        }
        req = _make_string_trigger_request(json.dumps(segment))
        resp = _make_string_trigger_response()
        handler.add_segment_callback(req, resp)
        assert len(handler.metadata.segments) == 1
        assert handler.metadata.segments[0].label_idx == 0

    def test_no_total_time_field(self, handler):
        """v2.0 does not have total_time_s."""
        segment = {
            "start_time": 10.0,
            "end_time": 20.0,
            "label_idx": 0,
            "success": True,
        }
        req = _make_string_trigger_request(json.dumps(segment))
        resp = _make_string_trigger_response()
        handler.add_segment_callback(req, resp)
        assert not hasattr(handler.metadata, "total_time_s")

    def test_invalid_json_no_change(self, handler):
        req = _make_string_trigger_request("not json")
        resp = _make_string_trigger_response()
        handler.add_segment_callback(req, resp)
        assert len(handler.metadata.segments) == 0
        assert resp.success is False


# ===================================================================
# TestRemoveLastSegment
# ===================================================================


class TestRemoveLastSegment:
    """Tests for ``remove_last_segment_callback``."""

    def _add_segment(self, handler, start, end):
        segment = {
            "start_time": start,
            "end_time": end,
            "label_idx": 0,
            "success": True,
        }
        req = _make_string_trigger_request(json.dumps(segment))
        resp = _make_string_trigger_response()
        handler.add_segment_callback(req, resp)

    def test_pops_segment(self, handler):
        self._add_segment(handler, 0.0, 10.0)
        assert len(handler.metadata.segments) == 1

        req = _make_trigger_request()
        resp = _make_trigger_response()
        handler.remove_last_segment_callback(req, resp)

        assert resp.success is True
        assert len(handler.metadata.segments) == 0

    def test_empty_segments_fails(self, handler):
        req = _make_trigger_request()
        resp = _make_trigger_response()
        handler.remove_last_segment_callback(req, resp)
        assert resp.success is False
        assert "No segments" in resp.message


# ===================================================================
# TestOverrideLastSegmentSuccess
# ===================================================================


class TestOverrideLastSegmentSuccess:
    """Tests for ``override_last_segment_success``."""

    def _add_segment(self, handler, success=False):
        segment = {
            "start_time": 0.0,
            "end_time": 1.0,
            "label_idx": 0,
            "success": success,
        }
        req = _make_string_trigger_request(json.dumps(segment))
        resp = _make_string_trigger_response()
        handler.add_segment_callback(req, resp)

    def test_override_true(self, handler):
        self._add_segment(handler, success=False)

        req = _make_string_trigger_request("true")
        resp = _make_string_trigger_response()
        handler.override_last_segment_success(req, resp)

        assert resp.success is True
        assert handler.metadata.segments[-1].success is True

    def test_override_false(self, handler):
        self._add_segment(handler, success=True)

        req = _make_string_trigger_request("false")
        resp = _make_string_trigger_response()
        handler.override_last_segment_success(req, resp)

        assert resp.success is True
        assert handler.metadata.segments[-1].success is False

    def test_no_segments_fails(self, handler):
        req = _make_string_trigger_request("true")
        resp = _make_string_trigger_response()
        handler.override_last_segment_success(req, resp)

        assert resp.success is False
        assert "No segments" in resp.message
