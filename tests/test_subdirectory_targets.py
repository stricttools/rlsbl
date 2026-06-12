"""Tests for per-target subdirectory paths (subdirectory targets feature).

Covers: _parse_target_entry, detect_targets with subdirectory config,
version sync with subdirectory targets, resolve_release_targets with
structured config, and _merge_template_vars with per-target paths.
"""

import json

import pytest

from conftest import make_ctx
from rlsbl.errors import ConfigError
from rlsbl.targets import TARGETS, TargetEntry, _parse_target_entry, detect_targets
from rlsbl.commands.release import resolve_release_targets, resolve_target_paths
from rlsbl.commands.init_cmd import _merge_template_vars


# ---------------------------------------------------------------------------
# Test class 1: _parse_target_entry
# ---------------------------------------------------------------------------


class TestParseTargetEntry:
    """Unit tests for _parse_target_entry from rlsbl.targets."""

    def test_string_entry(self, tmp_path):
        result = _parse_target_entry("npm", str(tmp_path))
        assert result == TargetEntry(name="npm", path=str(tmp_path))

    def test_dict_entry(self, tmp_path):
        subdir = tmp_path / "npm"
        subdir.mkdir()
        result = _parse_target_entry({"name": "npm", "path": "npm"}, str(tmp_path))
        assert result.name == "npm"
        assert result.path == str(subdir)

    def test_dict_entry_default_path(self, tmp_path):
        result = _parse_target_entry({"name": "npm"}, str(tmp_path))
        assert result.path == str(tmp_path)

    def test_dict_entry_missing_name(self, tmp_path):
        with pytest.raises(ConfigError):
            _parse_target_entry({"path": "npm"}, str(tmp_path))

    def test_invalid_type(self, tmp_path):
        with pytest.raises(TypeError):
            _parse_target_entry(42, str(tmp_path))

    def test_dict_entry_absolute_path(self, tmp_path):
        """Absolute paths in dict entries are used as-is."""
        abs_dir = tmp_path / "abs"
        abs_dir.mkdir()
        result = _parse_target_entry({"name": "npm", "path": str(abs_dir)}, str(tmp_path))
        assert result.path == str(abs_dir)


# ---------------------------------------------------------------------------
# Test class 2: detect_targets with subdirectory config
# ---------------------------------------------------------------------------


class TestDetectTargetsSubdirectory:
    """Integration tests for detect_targets() with subdirectory config."""

    def test_plain_string_config(self, tmp_path):
        """Plain string targets default to project root."""
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text('{"targets": ["npm"]}')
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        entries = detect_targets(str(tmp_path))
        assert len(entries) == 1
        assert entries[0].name == "npm"
        assert entries[0].path == str(tmp_path)

    def test_dict_config_with_path(self, tmp_path):
        """Dict targets resolve subdirectory paths."""
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        (config_dir / "config.json").write_text(
            '{"targets": [{"name": "npm", "path": "npm"}]}'
        )
        entries = detect_targets(str(tmp_path))
        assert len(entries) == 1
        assert entries[0].name == "npm"
        assert entries[0].path == str(npm_dir)

    def test_mixed_config(self, tmp_path):
        """Mix of plain strings and dict entries."""
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.21\n")
        (tmp_path / "VERSION").write_text("1.0.0")
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        (config_dir / "config.json").write_text(
            '{"targets": ["go", {"name": "npm", "path": "npm"}]}'
        )
        entries = detect_targets(str(tmp_path))
        assert len(entries) == 2
        assert entries[0] == TargetEntry("go", str(tmp_path))
        assert entries[1] == TargetEntry("npm", str(npm_dir))

    def test_auto_detection_returns_root_paths(self, tmp_path):
        """Without config, auto-detected targets use project root."""
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        entries = detect_targets(str(tmp_path))
        npm_entries = [e for e in entries if e.name == "npm"]
        assert len(npm_entries) == 1
        assert npm_entries[0].path == str(tmp_path)


# ---------------------------------------------------------------------------
# Test class 3: version sync with subdirectory targets
# ---------------------------------------------------------------------------


class TestVersionSyncSubdirectory:
    """Tests that version read/write/name/metadata work with subdirectory paths."""

    def test_read_version_from_subdirectory(self, tmp_path):
        """Target reads version from its subdirectory."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "2.0.0"}')
        assert TARGETS["npm"].read_version(str(npm_dir)) == "2.0.0"

    def test_write_version_to_subdirectory(self, tmp_path):
        """Target writes version to its subdirectory."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        TARGETS["npm"].write_version(str(npm_dir), "2.0.0", ctx=make_ctx(npm_dir))
        assert TARGETS["npm"].read_version(str(npm_dir)) == "2.0.0"

    def test_read_name_from_subdirectory(self, tmp_path):
        """read_name works with subdirectory path."""
        pypi_dir = tmp_path / "pypi"
        pypi_dir.mkdir()
        (pypi_dir / "pyproject.toml").write_text(
            '[project]\nname = "my-pkg"\nversion = "1.0.0"\n'
        )
        assert TARGETS["pypi"].read_name(str(pypi_dir), ctx=make_ctx(pypi_dir)) == "my-pkg"

    def test_read_metadata_from_subdirectory(self, tmp_path):
        """read_metadata works with subdirectory path."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text(
            '{"name": "test", "version": "1.0.0", "license": "MIT"}'
        )
        meta = TARGETS["npm"].read_metadata(str(npm_dir))
        assert meta.get("license") == "MIT"

    def test_go_version_in_subdirectory(self, tmp_path):
        """Go target reads version from VERSION file in subdirectory."""
        go_dir = tmp_path / "go"
        go_dir.mkdir()
        (go_dir / "go.mod").write_text("module example.com/test\n\ngo 1.21\n")
        (go_dir / "VERSION").write_text("3.0.0\n")
        assert TARGETS["go"].read_version(str(go_dir)) == "3.0.0"

    def test_pypi_write_version_in_subdirectory(self, tmp_path):
        """PyPI target writes version to pyproject.toml in subdirectory."""
        pypi_dir = tmp_path / "pypi"
        pypi_dir.mkdir()
        (pypi_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )
        TARGETS["pypi"].write_version(str(pypi_dir), "2.5.0", ctx=make_ctx(pypi_dir))
        assert TARGETS["pypi"].read_version(str(pypi_dir)) == "2.5.0"


# ---------------------------------------------------------------------------
# Test class 4: resolve_release_targets with structured config
# ---------------------------------------------------------------------------


class TestResolveReleaseTargetsSubdirectory:
    """Tests for resolve_release_targets() with subdirectory config."""

    def test_plain_string_release_targets(self, tmp_path, monkeypatch):
        """Plain string release_targets resolve to project_dir."""
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        config = {"targets": ["npm", "pypi"], "release_targets": ["npm"]}
        (config_dir / "config.json").write_text(json.dumps(config))
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        result = resolve_release_targets("pypi", {}, project_dir=str(tmp_path), config=config)
        assert isinstance(result, dict)
        assert "npm" in result

    def test_dict_release_targets(self, tmp_path, monkeypatch):
        """Dict release_targets resolve subdirectory paths."""
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        config = {"targets": [{"name": "npm", "path": "npm"}],
                  "release_targets": [{"name": "npm", "path": "npm"}]}
        (config_dir / "config.json").write_text(json.dumps(config))
        result = resolve_release_targets("pypi", {}, project_dir=str(tmp_path), config=config)
        assert "npm" in result
        assert result["npm"] == str(npm_dir)

    def test_resolve_target_paths_with_subdirs(self, tmp_path):
        """resolve_target_paths builds correct dict from subdirectory config."""
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        (config_dir / "config.json").write_text(
            '{"targets": ["go", {"name": "npm", "path": "npm"}]}'
        )
        result = resolve_target_paths(str(tmp_path))
        assert result["go"] == str(tmp_path)
        assert result["npm"] == str(npm_dir)



# ---------------------------------------------------------------------------
# Test class 6: _merge_template_vars with per-target paths
# ---------------------------------------------------------------------------


class TestMergeTemplateVarsSubdirectory:
    """Tests that _merge_template_vars works with per-target subdirectory paths."""

    def test_merge_with_subdirectory_paths(self, tmp_path):
        """Template vars merge correctly when targets live in different subdirectories."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        pypi_dir = tmp_path / "pypi"
        pypi_dir.mkdir()
        (npm_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        (pypi_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
            'requires-python = ">=3.11"\n'
        )
        target_paths = {"npm": str(npm_dir), "pypi": str(pypi_dir)}
        merged = _merge_template_vars(["npm", "pypi"], "npm", target_paths, str(tmp_path))
        # Primary (npm) vars are un-namespaced
        assert "name" in merged
        # Namespaced pypi vars
        assert "pypi.minRequiredPython" in merged
        assert merged["pypi.minRequiredPython"] == "3.11"

    def test_merge_primary_reads_from_subdirectory(self, tmp_path):
        """Primary target reads its vars from the correct subdirectory path."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text(
            '{"name": "subdir-pkg", "version": "1.0.0"}'
        )
        target_paths = {"npm": str(npm_dir)}
        merged = _merge_template_vars(["npm"], "npm", target_paths, str(tmp_path))
        assert merged["name"] == "subdir-pkg"
