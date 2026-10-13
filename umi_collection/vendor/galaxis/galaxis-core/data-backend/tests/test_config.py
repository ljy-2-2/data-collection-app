"""Tests for data_backend.config — strategy parser and config loader."""

import textwrap

import pytest

from data_backend.config import (
    PathRule,
    Priority,
    StorageConfig,
    load_storage_config,
    parse_gc_strategy,
)


# ---------------------------------------------------------------------------
# parse_gc_strategy
# ---------------------------------------------------------------------------


class TestParseGCStrategy:
    def test_single_marker(self):
        s = parse_gc_strategy(["marker"])
        assert s.evaluate(marker=True, age=False, space=False) is True
        assert s.evaluate(marker=False, age=True, space=True) is False

    def test_single_age(self):
        s = parse_gc_strategy(["age"])
        assert s.evaluate(marker=False, age=True, space=False) is True
        assert s.evaluate(marker=False, age=False, space=False) is False

    def test_marker_and_age(self):
        s = parse_gc_strategy(["marker", "age"])
        assert s.evaluate(marker=True, age=True, space=False) is True
        assert s.evaluate(marker=True, age=False, space=False) is False
        assert s.evaluate(marker=False, age=True, space=False) is False

    def test_marker_and_any_of_age_space(self):
        s = parse_gc_strategy(["marker", {"any_of": ["age", "space"]}])
        assert s.evaluate(marker=True, age=True, space=False) is True
        assert s.evaluate(marker=True, age=False, space=True) is True
        assert s.evaluate(marker=True, age=False, space=False) is False
        assert s.evaluate(marker=False, age=True, space=True) is False

    def test_all_three_and(self):
        s = parse_gc_strategy(["marker", "age", "space"])
        assert s.evaluate(marker=True, age=True, space=True) is True
        assert s.evaluate(marker=True, age=True, space=False) is False

    def test_any_of_all_three(self):
        s = parse_gc_strategy([{"any_of": ["marker", "age", "space"]}])
        assert s.evaluate(marker=True, age=False, space=False) is True
        assert s.evaluate(marker=False, age=False, space=True) is True
        assert s.evaluate(marker=False, age=False, space=False) is False

    def test_string_shorthand(self):
        s = parse_gc_strategy("marker")
        assert s.evaluate(marker=True, age=False, space=False) is True

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Invalid gc_strategy string"):
            parse_gc_strategy("combined")

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="non-empty list"):
            parse_gc_strategy([])

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown GC condition"):
            parse_gc_strategy(["marker", "bogus"])

    def test_unknown_in_any_of_raises(self):
        with pytest.raises(ValueError, match="Unknown GC condition"):
            parse_gc_strategy([{"any_of": ["marker", "bogus"]}])

    def test_empty_any_of_raises(self):
        with pytest.raises(ValueError, match="non-empty list"):
            parse_gc_strategy([{"any_of": []}])

    def test_bad_item_type_raises(self):
        with pytest.raises(ValueError, match="Invalid gc_strategy item"):
            parse_gc_strategy([42])

    def test_description_preserved(self):
        s = parse_gc_strategy(["marker", "age"])
        assert "marker" in s.description
        assert "age" in s.description


# ---------------------------------------------------------------------------
# load_storage_config
# ---------------------------------------------------------------------------


class TestLoadStorageConfig:
    def test_minimal_config(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              local:
                endpoint: "localhost:9000"
                access_key: "admin"
                secret_key: "secret"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        assert isinstance(cfg, StorageConfig)
        assert len(cfg.targets) == 1
        t = cfg.targets[0]
        assert t.name == "local"
        assert t.endpoint == "localhost:9000"
        assert t.priority == Priority.REQUIRED
        assert t.path_rule == PathRule.FLAT
        assert t.gc is None

    def test_defaults_inherited(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            defaults:
              bucket: "custom-bucket"
              path_rule: "canonical"
              priority: "preferred"
            targets:
              remote:
                endpoint: "remote:9000"
                access_key: "a"
                secret_key: "s"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        t = cfg.targets[0]
        assert t.bucket == "custom-bucket"
        assert t.path_rule == PathRule.CANONICAL
        assert t.priority == Priority.PREFERRED

    def test_target_overrides_defaults(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            defaults:
              bucket: "default-bucket"
              priority: "required"
            targets:
              archive:
                endpoint: "s3.aws.com"
                access_key: "a"
                secret_key: "s"
                bucket: "my-archive"
                priority: "optional"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        t = cfg.targets[0]
        assert t.bucket == "my-archive"
        assert t.priority == Priority.OPTIONAL

    def test_disabled_target_excluded(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              active:
                endpoint: "a:9000"
                access_key: "a"
                secret_key: "s"
              disabled:
                enabled: false
                endpoint: "b:9000"
                access_key: "a"
                secret_key: "s"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        assert len(cfg.targets) == 1
        assert cfg.targets[0].name == "active"

    def test_gc_config_parsed(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              local:
                endpoint: "localhost:9000"
                access_key: "a"
                secret_key: "s"
                gc:
                  strategy:
                    - marker
                    - age
                  max_age_hours: 48.0
                  max_storage_gb: 200.0
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        t = cfg.targets[0]
        assert t.gc is not None
        assert t.gc.max_age_hours == 48.0
        assert t.gc.max_storage_gb == 200.0
        assert t.gc.strategy.evaluate(marker=True, age=True, space=False) is True
        assert t.gc.strategy.evaluate(marker=True, age=False, space=False) is False

    def test_gc_none(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              archive:
                endpoint: "s3:9000"
                access_key: "a"
                secret_key: "s"
                gc: none
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        assert cfg.targets[0].gc is None

    def test_env_var_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_AK", "env-access-key")
        monkeypatch.setenv("TEST_SK", "env-secret-key")
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              cloud:
                endpoint: "s3.aws.com"
                access_key_env: "TEST_AK"
                secret_key_env: "TEST_SK"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        t = cfg.targets[0]
        assert t.access_key == "env-access-key"
        assert t.secret_key == "env-secret-key"

    def test_prefix_normalized(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            targets:
              local:
                endpoint: "localhost:9000"
                access_key: "a"
                secret_key: "s"
                prefix: "/robots/bot1/"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        assert cfg.targets[0].prefix == "robots/bot1/"

    def test_state_db_and_purge(self, tmp_path):
        cfg_file = tmp_path / "targets.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            state_db: "/tmp/test.db"
            state_purge_age_hours: 48
            targets:
              local:
                endpoint: "localhost:9000"
                access_key: "a"
                secret_key: "s"
        """)
        )
        cfg = load_storage_config(str(cfg_file))
        assert cfg.state_db == "/tmp/test.db"
        assert cfg.state_purge_age_hours == 48.0
