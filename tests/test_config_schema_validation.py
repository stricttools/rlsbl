"""Tests for consolidated config schema validation (validate_config_schema)."""

import json
import os

import pytest

from rlsbl.config import validate_config_schema
from rlsbl.errors import ConfigError


class TestBanEmptyTargets:
    """targets: [] must be a hard error."""

    def test_empty_targets_list_raises(self):
        config = {"targets": []}
        with pytest.raises(ConfigError, match="targets is an empty list"):
            validate_config_schema(config)

    def test_non_empty_targets_passes(self):
        config = {"targets": ["pypi"]}
        validate_config_schema(config)  # no error

    def test_missing_targets_key_passes(self):
        config = {"private": True}
        validate_config_schema(config)  # no error

    def test_targets_none_passes(self):
        config = {"targets": None}
        validate_config_schema(config)  # no error (None is not a list)


class TestBanReleaseMode:
    """release.mode key must be a hard error regardless of value."""

    def test_release_mode_imperative_raises(self):
        config = {"release": {"mode": "imperative"}}
        with pytest.raises(ConfigError, match="release.mode is no longer supported"):
            validate_config_schema(config)

    def test_release_mode_pr_raises(self):
        config = {"release": {"mode": "pr"}}
        with pytest.raises(ConfigError, match="release.mode is no longer supported"):
            validate_config_schema(config)

    def test_release_section_without_mode_passes(self):
        config = {"release": {"other_key": "value"}}
        validate_config_schema(config)  # no error

    def test_no_release_section_passes(self):
        config = {}
        validate_config_schema(config)  # no error

    def test_release_not_dict_passes(self):
        # Non-dict release values don't trigger mode check
        # (other validators handle the structural issue)
        config = {"release": "string_value"}
        validate_config_schema(config)  # no error


class TestBanStalePrModeState:
    """in-progress.json with release_mode: "pr" must be a hard error."""

    def test_pr_mode_state_file_raises(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        state_file = releases_dir / "in-progress.json"
        state_file.write_text(json.dumps({
            "release_mode": "pr",
            "completed_steps": ["VERSION_BUMPED"],
        }))
        config = {}
        with pytest.raises(ConfigError, match="stale PR-mode release state"):
            validate_config_schema(config, project_dir=str(tmp_path))

    def test_imperative_mode_state_file_passes(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        state_file = releases_dir / "in-progress.json"
        state_file.write_text(json.dumps({
            "release_mode": "imperative",
            "completed_steps": [],
        }))
        config = {}
        validate_config_schema(config, project_dir=str(tmp_path))  # no error

    def test_no_state_file_passes(self, tmp_path):
        config = {}
        validate_config_schema(config, project_dir=str(tmp_path))  # no error

    def test_no_project_dir_skips_state_check(self):
        config = {}
        validate_config_schema(config)  # no error without project_dir

    def test_malformed_state_file_passes(self, tmp_path):
        """Malformed JSON is not treated as a PR-mode violation."""
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        state_file = releases_dir / "in-progress.json"
        state_file.write_text("{not valid json}")
        config = {}
        validate_config_schema(config, project_dir=str(tmp_path))  # no error


class TestMultipleViolations:
    """First violation wins (raises immediately)."""

    def test_empty_targets_checked_before_release_mode(self):
        config = {"targets": [], "release": {"mode": "pr"}}
        with pytest.raises(ConfigError, match="targets is an empty list"):
            validate_config_schema(config)
