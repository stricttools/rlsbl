"""Tests for detect_targets() config inheritance via releasable_config_dir.

Covers:
- Inherited targets from releasable-level config
- Two-tier rule: no config.json -> auto-detect
- Two-tier rule: config.json exists but no targets key -> ConfigError with hints
- Explicit targets: [] returns empty list
- Explicit targets: ["pypi"] returns specified target
- Backward compatibility: detect_targets without releasable_config_dir
"""

import json
import os

import pytest

from rlsbl.errors import ConfigError
from rlsbl.targets import TargetEntry, detect_targets


class TestReleasableConfigInheritance:
    """detect_targets with releasable_config_dir reads inherited targets."""

    def test_inherits_targets_from_releasable_config(self, tmp_path):
        """When per-package config has no targets but releasable config does,
        detect_targets should use the releasable-level targets."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        # Per-package config.json with no targets key
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"private": False}))

        # Releasable-level config.json with targets
        rel_dir = tmp_path / "releasable"
        rel_dir.mkdir()
        (rel_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))

        # Create a pyproject.toml so pypi target validation passes
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )

        entries = detect_targets(str(pkg_dir), releasable_config_dir=str(rel_dir))
        assert len(entries) == 1
        assert entries[0].name == "pypi"

    def test_per_package_targets_override_releasable(self, tmp_path):
        """Per-package targets take precedence over releasable-level targets."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        # Per-package config with targets
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": ["npm"]})
        )

        # Releasable-level config with different targets
        rel_dir = tmp_path / "releasable"
        rel_dir.mkdir()
        (rel_dir / "config.json").write_text(json.dumps({"targets": ["pypi"]}))

        # Create package.json for npm target
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        entries = detect_targets(str(pkg_dir), releasable_config_dir=str(rel_dir))
        assert len(entries) == 1
        assert entries[0].name == "npm"

    def test_inherits_targets_from_releasable_publish_json(self, tmp_path):
        """Targets can also be inherited from releasable-level publish.json."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        # Per-package config.json with no targets key
        rlsbl_dir = pkg_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"private": False}))

        # Releasable-level publish.json with targets (targets is a PUBLISH_FIELD)
        rel_dir = tmp_path / "releasable"
        rel_dir.mkdir()
        (rel_dir / "config.json").write_text(json.dumps({}))
        (rel_dir / "publish.json").write_text(json.dumps({"targets": ["npm"]}))

        # Create package.json for npm target
        (pkg_dir / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        entries = detect_targets(str(pkg_dir), releasable_config_dir=str(rel_dir))
        assert len(entries) == 1
        assert entries[0].name == "npm"


class TestTwoTierRule:
    """Two-tier rule for missing targets key."""

    def test_no_config_json_auto_detects(self, tmp_path):
        """Without .rlsbl/config.json, auto-detect from manifests."""
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        entries = detect_targets(str(tmp_path))
        names = [e.name for e in entries]
        assert "npm" in names

    def test_config_exists_no_targets_key_raises(self, tmp_path):
        """Config exists but no targets key raises ConfigError with hints."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"private": False}))

        # Create a manifest so auto-detect would find something
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )

        with pytest.raises(ConfigError) as exc_info:
            detect_targets(str(tmp_path))

        # Error message should mention the detected targets as suggestions
        assert "pypi" in str(exc_info.value)
        assert "targets" in str(exc_info.value)

    def test_config_exists_no_targets_no_manifests_raises(self, tmp_path):
        """Config exists, no targets key, no manifests -> ConfigError."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"private": False}))

        with pytest.raises(ConfigError) as exc_info:
            detect_targets(str(tmp_path))

        assert "No targets could be auto-detected" in str(exc_info.value)


class TestExplicitTargets:
    """Tests for explicit targets configuration."""

    def test_empty_targets_list_returns_empty(self, tmp_path):
        """targets: [] explicitly means no targets."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": []})
        )

        # Even if a manifest exists, targets: [] returns empty
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )

        entries = detect_targets(str(tmp_path))
        assert entries == []

    def test_specified_target_returned(self, tmp_path):
        """targets: ["pypi"] returns the specified target."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": ["pypi"]})
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )

        entries = detect_targets(str(tmp_path))
        assert len(entries) == 1
        assert entries[0].name == "pypi"
        assert entries[0].path == str(tmp_path)


class TestBackwardCompatibility:
    """detect_targets without releasable_config_dir works for standalone."""

    def test_standalone_with_config_and_targets(self, tmp_path):
        """Standalone project with targets in config works without releasable_config_dir."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": ["npm"]})
        )
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        # Call without releasable_config_dir (defaults to None)
        entries = detect_targets(str(tmp_path))
        assert len(entries) == 1
        assert entries[0].name == "npm"

    def test_no_config_auto_detects_backward_compat(self, tmp_path):
        """Without config, auto-detection works as before."""
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        entries = detect_targets(str(tmp_path))
        names = [e.name for e in entries]
        assert "npm" in names

    def test_releasable_config_dir_none_works(self, tmp_path):
        """Explicitly passing releasable_config_dir=None works."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"private": False, "targets": ["npm"]})
        )
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )

        entries = detect_targets(str(tmp_path), releasable_config_dir=None)
        assert len(entries) == 1
        assert entries[0].name == "npm"
