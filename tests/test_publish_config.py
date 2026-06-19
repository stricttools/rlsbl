"""Tests for publish config separation (Phase 5 of releasable model redesign).

Covers read_publish_config, write_publish_config, PUBLISH_FIELDS,
conflict detection, backward compat, and migrate_publish_config.
"""

import json

import pytest

from rlsbl.config import (
    PUBLISH_FIELDS,
    migrate_publish_config,
    read_project_config,
    read_publish_config,
    write_publish_config,
)
from rlsbl.errors import ConfigError


def _write_json(path, data):
    """Write a dict as JSON to a path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _read_json(path):
    """Read a JSON file and return the parsed dict."""
    return json.loads(path.read_text())


class TestPublishFields:
    """PUBLISH_FIELDS constant covers the right fields."""

    def test_publish_fields_is_frozenset(self):
        assert isinstance(PUBLISH_FIELDS, frozenset)

    def test_publish_fields_contents(self):
        expected = {"targets", "private", "pipelines", "push_timeout", "tag"}
        assert PUBLISH_FIELDS == expected

    def test_non_publish_fields_excluded(self):
        non_publish = {"batch_limits", "env_file", "deploy", "release_branches"}
        assert PUBLISH_FIELDS & non_publish == set()


class TestReadPublishConfig:
    """read_publish_config -- file exists, file missing."""

    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        result = read_publish_config(tmp_path)
        assert result == {}

    def test_reads_existing_publish_json(self, tmp_path):
        publish_path = tmp_path / ".rlsbl" / "publish.json"
        data = {"private": False, "targets": ["pypi"]}
        _write_json(publish_path, data)

        result = read_publish_config(tmp_path)
        assert result == data

    def test_raises_on_malformed_json(self, tmp_path):
        publish_path = tmp_path / ".rlsbl" / "publish.json"
        publish_path.parent.mkdir(parents=True, exist_ok=True)
        publish_path.write_text("{bad json!!")

        with pytest.raises(ConfigError, match="Malformed JSON"):
            read_publish_config(tmp_path)


class TestWritePublishConfig:
    """write_publish_config -- round-trip, atomic write."""

    def test_round_trip(self, tmp_path):
        data = {
            "private": False,
            "targets": ["pypi", "npm"],
            "pipelines": {"pypi": {"type": "pypi", "local": False}},
            "push_timeout": 300,
            "tag": True,
        }
        write_publish_config(tmp_path, data)
        result = read_publish_config(tmp_path)
        assert result == data

    def test_creates_directory_if_missing(self, tmp_path):
        data = {"private": True}
        write_publish_config(tmp_path, data)
        assert (tmp_path / ".rlsbl" / "publish.json").exists()

    def test_overwrites_existing_file(self, tmp_path):
        write_publish_config(tmp_path, {"private": True})
        write_publish_config(tmp_path, {"private": False, "tag": True})
        result = read_publish_config(tmp_path)
        assert result == {"private": False, "tag": True}

    def test_atomic_write_no_tmp_file_left(self, tmp_path):
        write_publish_config(tmp_path, {"private": True})
        tmp_file = tmp_path / ".rlsbl" / "publish.json.tmp"
        assert not tmp_file.exists()

    def test_writes_trailing_newline(self, tmp_path):
        write_publish_config(tmp_path, {"private": True})
        content = (tmp_path / ".rlsbl" / "publish.json").read_text()
        assert content.endswith("\n")


class TestConflictDetection:
    """Both files have publishing fields -- hard error."""

    def test_conflict_raises_config_error(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"private": True, "batch_limits": {}})
        _write_json(publish_path, {"private": False})

        with pytest.raises(ConfigError, match="Publishing fields found in .rlsbl/config.json while .rlsbl/publish.json exists"):
            read_project_config(tmp_path)

    def test_conflict_mentions_overlapping_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"private": True, "targets": ["pypi"]})
        _write_json(publish_path, {"tag": True})

        with pytest.raises(ConfigError, match="private.*targets"):
            read_project_config(tmp_path)

    def test_no_conflict_when_config_has_only_non_publish_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"batch_limits": {"max_commits_per_entry": 5}})
        _write_json(publish_path, {"private": False})

        result = read_project_config(tmp_path)
        assert result["private"] is False
        assert result["batch_limits"] == {"max_commits_per_entry": 5}


class TestBackwardCompat:
    """Only config.json has publishing fields -- works (pre-migration)."""

    def test_reads_publish_fields_from_config_json(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        data = {
            "private": False,
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False}},
            "batch_limits": {"max_commits_per_entry": 5},
        }
        _write_json(config_path, data)

        result = read_project_config(tmp_path)
        assert result == data

    def test_config_json_only_no_error(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_json(config_path, {"private": True, "tag": False})

        # Should not raise
        result = read_project_config(tmp_path)
        assert result["private"] is True
        assert result["tag"] is False

    def test_empty_config_returns_empty_dict(self, tmp_path):
        result = read_project_config(tmp_path)
        assert result == {}


class TestForwardPath:
    """Only publish.json has publishing fields -- works."""

    def test_publish_json_only(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"batch_limits": {}})
        _write_json(publish_path, {"private": False, "targets": ["npm"]})

        result = read_project_config(tmp_path)
        assert result["private"] is False
        assert result["targets"] == ["npm"]
        assert result["batch_limits"] == {}

    def test_publish_json_with_no_config_json(self, tmp_path):
        publish_path = tmp_path / ".rlsbl" / "publish.json"
        _write_json(publish_path, {"private": True})

        result = read_project_config(tmp_path)
        assert result["private"] is True

    def test_merged_view_has_all_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {
            "batch_limits": {"max_commits_per_entry": 5},
            "env_file": "~/Projects/.env",
            "release_branches": ["main"],
        })
        _write_json(publish_path, {
            "private": False,
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False}},
            "push_timeout": 180,
            "tag": True,
        })

        result = read_project_config(tmp_path)
        # All fields present
        assert result["batch_limits"] == {"max_commits_per_entry": 5}
        assert result["env_file"] == "~/Projects/.env"
        assert result["release_branches"] == ["main"]
        assert result["private"] is False
        assert result["targets"] == ["pypi"]
        assert result["pipelines"] == {"pypi": {"type": "pypi", "local": False}}
        assert result["push_timeout"] == 180
        assert result["tag"] is True


class TestMigration:
    """migrate_publish_config splits config correctly."""

    def test_extracts_publish_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_json(config_path, {
            "private": False,
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False}},
            "push_timeout": 180,
            "tag": True,
            "batch_limits": {"max_commits_per_entry": 5},
            "env_file": "~/Projects/.env",
        })

        extracted, remaining = migrate_publish_config(tmp_path)

        assert extracted == {
            "private": False,
            "targets": ["pypi"],
            "pipelines": {"pypi": {"type": "pypi", "local": False}},
            "push_timeout": 180,
            "tag": True,
        }
        assert remaining == {
            "batch_limits": {"max_commits_per_entry": 5},
            "env_file": "~/Projects/.env",
        }

    def test_files_valid_after_migration(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {
            "private": False,
            "targets": ["pypi"],
            "batch_limits": {"max_commits_per_entry": 5},
        })

        migrate_publish_config(tmp_path)

        # Both files should be valid JSON
        config_data = _read_json(config_path)
        publish_data = _read_json(publish_path)

        # Publishing fields in publish.json only
        assert "private" in publish_data
        assert "targets" in publish_data
        assert "private" not in config_data
        assert "targets" not in config_data

        # Non-publishing fields remain in config.json
        assert "batch_limits" in config_data
        assert "batch_limits" not in publish_data

    def test_read_project_config_works_after_migration(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        original = {
            "private": False,
            "targets": ["pypi"],
            "batch_limits": {"max_commits_per_entry": 5},
        }
        _write_json(config_path, original)

        migrate_publish_config(tmp_path)

        # read_project_config should return the merged view identical to original
        result = read_project_config(tmp_path)
        assert result == original

    def test_noop_when_no_publish_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_json(config_path, {
            "batch_limits": {"max_commits_per_entry": 5},
            "env_file": "~/Projects/.env",
        })

        extracted, remaining = migrate_publish_config(tmp_path)

        assert extracted == {}
        assert remaining == {
            "batch_limits": {"max_commits_per_entry": 5},
            "env_file": "~/Projects/.env",
        }
        # publish.json should not be created
        assert not (tmp_path / ".rlsbl" / "publish.json").exists()

    def test_errors_if_publish_json_already_has_content(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        publish_path = tmp_path / ".rlsbl" / "publish.json"

        _write_json(config_path, {"private": True})
        _write_json(publish_path, {"tag": False})

        with pytest.raises(ConfigError, match="already exists and has content"):
            migrate_publish_config(tmp_path)

    def test_migration_with_only_some_publish_fields(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_json(config_path, {
            "private": True,
            "batch_limits": {},
        })

        extracted, remaining = migrate_publish_config(tmp_path)

        assert extracted == {"private": True}
        assert remaining == {"batch_limits": {}}

        # Verify files on disk
        publish_data = _read_json(tmp_path / ".rlsbl" / "publish.json")
        config_data = _read_json(config_path)
        assert publish_data == {"private": True}
        assert config_data == {"batch_limits": {}}

    def test_migration_preserves_field_values(self, tmp_path):
        config_path = tmp_path / ".rlsbl" / "config.json"
        pipelines_data = {
            "pypi": {"type": "pypi", "local": False, "assets": True, "max_asset_size_mb": 50},
            "npm": {"type": "npm", "local": True},
        }
        _write_json(config_path, {
            "private": False,
            "targets": ["pypi", "npm"],
            "pipelines": pipelines_data,
            "push_timeout": 300,
            "tag": False,
            "batch_limits": {"max_commits_per_entry": 10, "exclusions": []},
            "deploy": [{"type": "cloudflare-pages"}],
            "release_branches": ["main", "release/*"],
        })

        extracted, remaining = migrate_publish_config(tmp_path)

        assert extracted["pipelines"] == pipelines_data
        assert extracted["push_timeout"] == 300
        assert extracted["tag"] is False
        assert remaining["deploy"] == [{"type": "cloudflare-pages"}]
        assert remaining["release_branches"] == ["main", "release/*"]
