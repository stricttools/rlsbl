"""Tests for consolidated config schema validation (validate_config_schema)."""

import pytest

from rlsbl import app
from rlsbl.config import validate_config_schema, validate_test_config
from rlsbl.context import ProjectContext
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


class TestMultipleViolations:
    """First violation wins (raises immediately)."""

    def test_empty_targets_checked_before_release_mode(self):
        config = {"targets": [], "release": {"mode": "pr"}}
        with pytest.raises(ConfigError, match="targets is an empty list"):
            validate_config_schema(config)


class TestValidateTestConfig:
    """Per-target ``test`` section validation (validate_test_config)."""

    def test_absent_test_section_passes(self):
        validate_test_config({"private": True})  # no error

    def test_test_none_passes(self):
        validate_test_config({"test": None})  # no error (None is not a dict)

    def test_valid_pypi_markers_passes(self):
        validate_test_config({"test": {"pypi": {"markers": "not integration"}}})

    def test_empty_pypi_block_passes(self):
        # A target block with no options is valid -- run everything.
        validate_test_config({"test": {"pypi": {}}})

    def test_test_not_dict_raises(self):
        with pytest.raises(ConfigError, match="test must be a dict"):
            validate_test_config({"test": "nope"})

    def test_unknown_target_raises(self):
        with pytest.raises(ConfigError, match="not a recognized test target"):
            validate_test_config({"test": {"rust": {"markers": "x"}}})

    def test_target_block_not_dict_raises(self):
        with pytest.raises(ConfigError, match="must be a dict"):
            validate_test_config({"test": {"pypi": "not integration"}})

    def test_unknown_inner_key_raises(self):
        # A typo like "marker" (singular) must be rejected, not silently ignored.
        with pytest.raises(ConfigError, match="not a recognized option"):
            validate_test_config({"test": {"pypi": {"marker": "not integration"}}})

    def test_non_string_markers_raises(self):
        with pytest.raises(ConfigError, match="markers must be a string"):
            validate_test_config({"test": {"pypi": {"markers": ["not", "integration"]}}})

    def test_empty_string_markers_raises(self):
        with pytest.raises(ConfigError, match="empty string"):
            validate_test_config({"test": {"pypi": {"markers": ""}}})


class TestConfigSchemaCheckSurfacesTestConfig:
    """The config-schema check surfaces validate_test_config failures."""

    def test_bad_test_block_fails_check(self, tmp_project):
        ctx = ProjectContext(
            project_root=tmp_project,
            workspace_root=None,
            config={"private": True, "test": {"pypi": {"marker": "not integration"}}},
        )
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "fail"
        assert any("not a recognized option" in d for d in result.details)

    def test_valid_test_block_passes_check(self, tmp_project):
        ctx = ProjectContext(
            project_root=tmp_project,
            workspace_root=None,
            config={"private": True, "test": {"pypi": {"markers": "not integration"}}},
        )
        result = app._check_defs["config-schema"].impl(ctx)
        assert result.status == "pass"
