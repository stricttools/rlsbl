"""Tests for rlsbl.config."""

import json

import pytest

from rlsbl.config import read_json_config, should_tag, write_project_config
from rlsbl.errors import ConfigError


class TestShouldTag:
    """Tests for should_tag precedence logic."""

    def test_returns_true_with_empty_flags_and_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No config, empty flags -> default True
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(tmp_path / "nope.json"))
        assert should_tag({}, {}) is True

    def test_returns_false_when_no_tag_flag_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert should_tag({"auto-tag": False}, {}) is False

    def test_reads_project_config_tag_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(tmp_path / "nope.json"))
        assert should_tag({}, {"tag": False}) is False

    def test_reads_user_config_as_fallback(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # User config says tag: false
        user_config = tmp_path / "user_config.json"
        user_config.write_text(json.dumps({"tag": False}))
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(user_config))
        # Empty project config -- should fall through to user config
        assert should_tag({}, {}) is False

    def test_project_config_overrides_user_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # User config says tag: false
        user_config = tmp_path / "user_config.json"
        user_config.write_text(json.dumps({"tag": False}))
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(user_config))
        # Project config says tag: true -- should override user config
        assert should_tag({}, {"tag": True}) is True


class TestReadJsonConfig:
    """Tests for read_json_config edge cases."""

    def test_returns_empty_dict_on_missing_file(self, tmp_path):
        assert read_json_config(str(tmp_path / "missing.json")) == {}

    def test_raises_value_error_on_malformed_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json!!")
        with pytest.raises(ConfigError, match="Malformed JSON"):
            read_json_config(str(bad_file))


class TestWriteProjectConfig:
    """Tests for write_project_config."""

    def test_creates_dir_and_file_and_preserves_existing_keys(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_path = str(tmp_path / ".rlsbl" / "config.json")
        monkeypatch.setattr("rlsbl.config._project_config", lambda project_root: config_path)

        # First write
        write_project_config("tag", False, str(tmp_path))
        data = json.loads(open(config_path).read())
        assert data == {"tag": False}

        # Second write should preserve "tag" key
        write_project_config("other_key", "hello", str(tmp_path))
        data = json.loads(open(config_path).read())
        assert data == {"tag": False, "other_key": "hello"}

    def test_write_is_atomic_preserves_original_on_failure(self, tmp_path):
        """A failure mid-write must not corrupt the existing config.json."""
        from unittest import mock

        config_path = tmp_path / ".rlsbl" / "config.json"
        config_path.parent.mkdir(parents=True)
        original = {"tag": True, "existing": "value"}
        config_path.write_text(json.dumps(original) + "\n")

        with mock.patch("rlsbl.config.json.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                write_project_config("new_key", "x", str(tmp_path))

        # Original file must be intact -- not truncated or corrupted.
        assert json.loads(config_path.read_text()) == original
        # No leftover tmp files in the .rlsbl dir.
        leftovers = sorted(
            p.name for p in config_path.parent.iterdir() if p.name != "config.json"
        )
        assert leftovers == []

    def test_write_success_leaves_no_tmp_files(self, tmp_path):
        """A successful write produces correct content and no tmp residue."""
        config_path = tmp_path / ".rlsbl" / "config.json"

        write_project_config("tag", False, str(tmp_path))

        assert json.loads(config_path.read_text()) == {"tag": False}
        leftovers = sorted(p.name for p in config_path.parent.iterdir())
        assert leftovers == ["config.json"]
