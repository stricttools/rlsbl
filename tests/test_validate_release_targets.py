"""Direct unit tests for validate_release_targets.

Previously this function was always mocked in integration tests. These tests
exercise it directly with real filesystem state (tmp_path projects) and
verify both the single-project and releasable (member_dirs) code paths.
"""

import json
import os

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_release_targets,
)
from rlsbl.release_file import ReleaseConfig


def _make_npm_project(path):
    """Create a minimal npm project at path."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "package.json"), "w") as f:
        json.dump({"name": "test", "version": "1.0.0"}, f)


def _make_pypi_project(path):
    """Create a minimal pypi project at path."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "test"\nversion = "0.1.0"\n')


def _make_go_project(path):
    """Create a minimal Go project at path."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "go.mod"), "w") as f:
        f.write("module example.com/test\n\ngo 1.21\n")


class TestSingleProject:
    """Tests for the non-releasable (single project) code path."""

    def test_include_covers_detected_targets(self, tmp_path):
        """When include covers all detected targets, returns the registry (first include item)."""
        _make_npm_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=[],
        )
        result = validate_release_targets(config, tmp_path)
        assert result == "npm"

    def test_detected_target_not_in_include_raises(self, tmp_path):
        """When a detected target is missing from include+exclude, raises ReleaseValidationError."""
        _make_npm_project(str(tmp_path))
        _make_pypi_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=[],
        )
        with pytest.raises(ReleaseValidationError, match="detected targets not in release file"):
            validate_release_targets(config, tmp_path)

    def test_detected_target_in_exclude_is_ok(self, tmp_path):
        """Targets in exclude satisfy the exhaustiveness check."""
        _make_npm_project(str(tmp_path))
        _make_pypi_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=["pypi"],
        )
        result = validate_release_targets(config, tmp_path)
        assert result == "npm"

    def test_empty_include_raises(self, tmp_path):
        """Empty include list raises ReleaseValidationError."""
        _make_npm_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=[],
            exclude=["npm"],
        )
        with pytest.raises(ReleaseValidationError, match="empty include list"):
            validate_release_targets(config, tmp_path)

    def test_unknown_target_raises(self, tmp_path):
        """Unknown target name in include raises ReleaseValidationError."""
        _make_npm_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=["npm", "nonexistent_target"],
            exclude=[],
        )
        with pytest.raises(ReleaseValidationError, match="unknown target"):
            validate_release_targets(config, tmp_path)

    def test_extra_targets_in_include_warns_no_error(self, tmp_path, capsys):
        """Targets in include/exclude that are not detected produce a warning, not an error."""
        _make_npm_project(str(tmp_path))

        config = ReleaseConfig(
            bump="patch",
            include=["npm"],
            exclude=["pypi"],
        )
        result = validate_release_targets(config, tmp_path)
        assert result == "npm"

        captured = capsys.readouterr()
        assert "not detected in project" in captured.err
        assert "pypi" in captured.err


class TestReleasableWithMemberDirs:
    """Tests for the releasable (member_dirs) code path."""

    def test_mixed_targets_all_covered(self, tmp_path):
        """Releasable with pypi + go members, include covers both, returns first include item."""
        member_a = tmp_path / "pkg-a"
        member_b = tmp_path / "pkg-b"
        _make_pypi_project(str(member_a))
        _make_go_project(str(member_b))

        config = ReleaseConfig(
            bump="patch",
            include=["pypi", "go"],
            exclude=[],
        )
        result = validate_release_targets(
            config, tmp_path,
            member_dirs=[str(member_a), str(member_b)],
        )
        assert result == "pypi"

    def test_mixed_targets_missing_one_raises(self, tmp_path):
        """Releasable with pypi + go members, include only covers pypi, raises error."""
        member_a = tmp_path / "pkg-a"
        member_b = tmp_path / "pkg-b"
        _make_pypi_project(str(member_a))
        _make_go_project(str(member_b))

        config = ReleaseConfig(
            bump="patch",
            include=["pypi"],
            exclude=[],
        )
        with pytest.raises(ReleaseValidationError, match="detected targets not in release file"):
            validate_release_targets(
                config, tmp_path,
                member_dirs=[str(member_a), str(member_b)],
            )

    def test_releasable_config_targets_take_precedence(self, tmp_path):
        """When releasable_config_dir has targets key, those are used instead of member detection."""
        member_a = tmp_path / "pkg-a"
        _make_npm_project(str(member_a))

        # Releasable config declares pypi as the target (overrides npm detection)
        rel_config_dir = tmp_path / "rel-config"
        rel_config_dir.mkdir()
        with open(rel_config_dir / "config.json", "w") as f:
            json.dump({"targets": ["pypi"]}, f)

        config = ReleaseConfig(
            bump="patch",
            include=["pypi"],
            exclude=[],
        )
        result = validate_release_targets(
            config, tmp_path,
            member_dirs=[str(member_a)],
            releasable_config_dir=str(rel_config_dir),
        )
        assert result == "pypi"

    def test_extra_targets_in_releasable_warns_no_error(self, tmp_path, capsys):
        """Extra targets declared in include/exclude but not detected produce a warning."""
        member_a = tmp_path / "pkg-a"
        _make_pypi_project(str(member_a))

        config = ReleaseConfig(
            bump="patch",
            include=["pypi"],
            exclude=["go"],
        )
        result = validate_release_targets(
            config, tmp_path,
            member_dirs=[str(member_a)],
        )
        assert result == "pypi"

        captured = capsys.readouterr()
        assert "not detected in project" in captured.err
        assert "go" in captured.err


# ---------------------------------------------------------------------------
# resolve_changes_dir
# ---------------------------------------------------------------------------

class TestResolveChangesDir:
    """Tests for resolve_changes_dir path resolution."""

    def test_returns_changes_dir_for_standalone_project(self, tmp_path):
        """Standalone project with .rlsbl/changes/ returns the correct path."""
        from rlsbl.commands.release.validate import resolve_changes_dir

        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        result = resolve_changes_dir(str(tmp_path))
        assert result == str(changes)

    def test_raises_when_changes_dir_missing(self, tmp_path):
        """Raises ReleaseValidationError when .rlsbl/changes/ does not exist."""
        from rlsbl.commands.release.validate import resolve_changes_dir

        with pytest.raises(ReleaseValidationError, match="JSONL changelog not set up"):
            resolve_changes_dir(str(tmp_path))

    def test_returns_releasable_changes_dir(self, tmp_path):
        """In explicit releasable mode, returns the releasable-level changes dir."""
        from rlsbl.commands.release.validate import resolve_changes_dir

        rel_changes = tmp_path / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        rel_changes.mkdir(parents=True)

        result = resolve_changes_dir(
            str(tmp_path / "packages" / "core"),
            releasable_name="core",
            workspace_root=str(tmp_path),
        )
        assert result == str(rel_changes)

    def test_raises_when_releasable_changes_dir_missing(self, tmp_path):
        """Raises ReleaseValidationError when releasable changes dir is missing."""
        from rlsbl.commands.release.validate import resolve_changes_dir

        with pytest.raises(ReleaseValidationError, match="not set up for releasable"):
            resolve_changes_dir(
                str(tmp_path / "packages" / "core"),
                releasable_name="core",
                workspace_root=str(tmp_path),
            )
