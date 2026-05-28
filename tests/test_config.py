"""Tests for rlsbl.config."""

import json

import pytest

from rlsbl.config import read_json_config, should_tag, write_project_config


class TestShouldTag:
    """Tests for should_tag precedence logic."""

    def test_returns_true_with_empty_flags_and_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No config files exist, empty flags -> default True
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(tmp_path / "nope.json"))
        assert should_tag({}) is True

    def test_returns_false_when_no_tag_flag_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert should_tag({"no-tag": True}) is False

    def test_reads_project_config_tag_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(tmp_path / "nope.json"))
        # Create project config with tag: false
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"tag": False}))
        monkeypatch.setattr("rlsbl.config._project_config", lambda project_root=None: str(config_dir / "config.json"))
        assert should_tag({}) is False

    def test_reads_user_config_as_fallback(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No project config
        monkeypatch.setattr("rlsbl.config._project_config", lambda project_root=None: str(tmp_path / "no_project.json"))
        # User config says tag: false
        user_config = tmp_path / "user_config.json"
        user_config.write_text(json.dumps({"tag": False}))
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(user_config))
        assert should_tag({}) is False

    def test_project_config_overrides_user_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Project config says tag: true
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"tag": True}))
        monkeypatch.setattr("rlsbl.config._project_config", lambda project_root=None: str(config_dir / "config.json"))
        # User config says tag: false
        user_config = tmp_path / "user_config.json"
        user_config.write_text(json.dumps({"tag": False}))
        monkeypatch.setattr("rlsbl.config.USER_CONFIG", str(user_config))
        assert should_tag({}) is True


class TestReadJsonConfig:
    """Tests for read_json_config edge cases."""

    def test_returns_empty_dict_on_missing_file(self, tmp_path):
        assert read_json_config(str(tmp_path / "missing.json")) == {}

    def test_returns_empty_dict_on_malformed_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json!!")
        assert read_json_config(str(bad_file)) == {}


class TestWriteProjectConfig:
    """Tests for write_project_config."""

    def test_creates_dir_and_file_and_preserves_existing_keys(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_path = str(tmp_path / ".rlsbl" / "config.json")
        monkeypatch.setattr("rlsbl.config._project_config", lambda project_root=None: config_path)

        # First write
        write_project_config("tag", False)
        data = json.loads(open(config_path).read())
        assert data == {"tag": False}

        # Second write should preserve "tag" key
        write_project_config("other_key", "hello")
        data = json.loads(open(config_path).read())
        assert data == {"tag": False, "other_key": "hello"}
