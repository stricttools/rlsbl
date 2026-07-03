"""Tests for releasable directory structure and version management (Phase 3).

Covers:
- Path resolution utilities (get_releasable_dir, get_releasable_version_path)
- Version read/write round-trip and atomic write behavior
- Missing/empty version file errors
- is_explicit_mode detection
- compute_release_version with releasable version source
- version-consistency check with and without releasables
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    RELEASABLES_DIR,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    get_releasable_dir,
    get_releasable_version_path,
    is_explicit_mode,
    read_releasable_version,
    write_releasable_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _init_git(tmp_path):
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)
    readme = tmp_path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(tmp_path), check=True)


# ---------------------------------------------------------------------------
# Path resolution: get_releasable_dir
# ---------------------------------------------------------------------------


class TestGetReleasableDir:
    """get_releasable_dir returns the correct directory path."""

    def test_basic_path(self, tmp_path):
        result = get_releasable_dir(str(tmp_path), "core")
        expected = os.path.join(str(tmp_path), WORKSPACE_DIR, RELEASABLES_DIR, "core")
        assert result == expected

    def test_different_names(self, tmp_path):
        for name in ("core", "www", "my-app", "test_infra"):
            result = get_releasable_dir(str(tmp_path), name)
            assert result.endswith(os.path.join(RELEASABLES_DIR, name))

    def test_path_components(self, tmp_path):
        result = get_releasable_dir(str(tmp_path), "core")
        parts = result.split(os.sep)
        assert ".rlsbl-monorepo" in parts
        assert "releasables" in parts
        assert "core" in parts


# ---------------------------------------------------------------------------
# Path resolution: get_releasable_version_path
# ---------------------------------------------------------------------------


class TestGetReleasableVersionPath:
    """get_releasable_version_path returns path to the version file."""

    def test_basic_path(self, tmp_path):
        result = get_releasable_version_path(str(tmp_path), "core")
        expected = os.path.join(
            str(tmp_path), WORKSPACE_DIR, RELEASABLES_DIR, "core", "version"
        )
        assert result == expected

    def test_consistent_with_dir(self, tmp_path):
        dir_path = get_releasable_dir(str(tmp_path), "www")
        version_path = get_releasable_version_path(str(tmp_path), "www")
        assert version_path == os.path.join(dir_path, "version")


# ---------------------------------------------------------------------------
# Version read/write round-trip
# ---------------------------------------------------------------------------


class TestVersionReadWrite:
    """read_releasable_version and write_releasable_version round-trip."""

    def test_write_then_read(self, tmp_path):
        write_releasable_version(str(tmp_path), "core", "1.2.3")
        result = read_releasable_version(str(tmp_path), "core")
        assert result == "1.2.3"

    def test_overwrite_version(self, tmp_path):
        write_releasable_version(str(tmp_path), "core", "0.1.0")
        assert read_releasable_version(str(tmp_path), "core") == "0.1.0"

        write_releasable_version(str(tmp_path), "core", "0.2.0")
        assert read_releasable_version(str(tmp_path), "core") == "0.2.0"

    def test_multiple_releasables(self, tmp_path):
        write_releasable_version(str(tmp_path), "core", "1.0.0")
        write_releasable_version(str(tmp_path), "www", "2.0.0")

        assert read_releasable_version(str(tmp_path), "core") == "1.0.0"
        assert read_releasable_version(str(tmp_path), "www") == "2.0.0"

    def test_version_with_prerelease(self, tmp_path):
        write_releasable_version(str(tmp_path), "core", "1.0.0-rc.1")
        assert read_releasable_version(str(tmp_path), "core") == "1.0.0-rc.1"

    def test_version_strips_whitespace(self, tmp_path):
        """read_releasable_version strips whitespace from the version."""
        version_path = get_releasable_version_path(str(tmp_path), "core")
        os.makedirs(os.path.dirname(version_path), exist_ok=True)
        with open(version_path, "w") as f:
            f.write("  1.2.3  \n\n")
        assert read_releasable_version(str(tmp_path), "core") == "1.2.3"

    def test_write_creates_directory(self, tmp_path):
        """write_releasable_version creates the releasable directory if needed."""
        rel_dir = get_releasable_dir(str(tmp_path), "newrel")
        assert not os.path.exists(rel_dir)

        write_releasable_version(str(tmp_path), "newrel", "0.0.1")
        assert os.path.isdir(rel_dir)

    def test_file_ends_with_newline(self, tmp_path):
        """Version file ends with a trailing newline."""
        write_releasable_version(str(tmp_path), "core", "1.0.0")
        version_path = get_releasable_version_path(str(tmp_path), "core")
        with open(version_path, "r") as f:
            content = f.read()
        assert content == "1.0.0\n"


# ---------------------------------------------------------------------------
# Atomic write behavior
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """write_releasable_version uses atomic write (tempfile + os.replace)."""

    def test_no_partial_write_on_error(self, tmp_path):
        """If writing fails, no version file should be created."""
        # Make the directory read-only to cause a write failure
        rel_dir = get_releasable_dir(str(tmp_path), "fail")
        os.makedirs(rel_dir, exist_ok=True)
        # Write a valid version first
        write_releasable_version(str(tmp_path), "fail", "1.0.0")

        # Make directory read-only to prevent tempfile creation
        os.chmod(rel_dir, 0o555)
        try:
            with pytest.raises(OSError):
                write_releasable_version(str(tmp_path), "fail", "2.0.0")
            # Original version should still be intact
            assert read_releasable_version(str(tmp_path), "fail") == "1.0.0"
        finally:
            os.chmod(rel_dir, 0o755)

    def test_no_temp_files_left_behind(self, tmp_path):
        """After a successful write, no temp files remain."""
        write_releasable_version(str(tmp_path), "core", "1.0.0")
        rel_dir = get_releasable_dir(str(tmp_path), "core")
        files = os.listdir(rel_dir)
        assert files == ["version"], f"unexpected files: {files}"


# ---------------------------------------------------------------------------
# Missing version file error
# ---------------------------------------------------------------------------


class TestReadVersionErrors:
    """read_releasable_version raises WorkspaceError on missing/empty file."""

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(WorkspaceError, match="version file missing"):
            read_releasable_version(str(tmp_path), "nonexistent")

    def test_empty_file_raises(self, tmp_path):
        version_path = get_releasable_version_path(str(tmp_path), "core")
        os.makedirs(os.path.dirname(version_path), exist_ok=True)
        with open(version_path, "w") as f:
            f.write("")
        with pytest.raises(WorkspaceError, match="version file is empty"):
            read_releasable_version(str(tmp_path), "core")

    def test_whitespace_only_file_raises(self, tmp_path):
        version_path = get_releasable_version_path(str(tmp_path), "core")
        os.makedirs(os.path.dirname(version_path), exist_ok=True)
        with open(version_path, "w") as f:
            f.write("   \n\n  ")
        with pytest.raises(WorkspaceError, match="version file is empty"):
            read_releasable_version(str(tmp_path), "core")


# ---------------------------------------------------------------------------
# is_explicit_mode
# ---------------------------------------------------------------------------


class TestIsExplicitMode:
    """is_explicit_mode detects [[releasables]] in workspace.toml."""

    def test_explicit_mode_with_releasables(self, tmp_path):
        _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        assert is_explicit_mode(str(tmp_path)) is True

    def test_no_releasables_section(self, tmp_path):
        _write_workspace(tmp_path, """\
[[projects]]
path = "a"
name = "a"
""")
        assert is_explicit_mode(str(tmp_path)) is False

    def test_no_workspace_file(self, tmp_path):
        assert is_explicit_mode(str(tmp_path)) is False

    def test_empty_releasables_is_explicit(self, tmp_path):
        """An empty releasables array is still explicit mode."""
        _write_workspace(tmp_path, """\
releasables = []

[[projects]]
path = "a"
name = "a"
releasable = false
""")
        assert is_explicit_mode(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# compute_release_version with releasable
# ---------------------------------------------------------------------------


class TestComputeReleaseVersionReleasable:
    """compute_release_version reads from releasable version file in explicit mode."""

    def test_reads_from_releasable_version_file(self, tmp_path):
        """When workspace_root and releasable_name are given, version comes from
        the releasable version file, not the target manifest."""
        _init_git(tmp_path)

        # Write releasable version
        write_releasable_version(str(tmp_path), "core", "1.5.0")

        # Create a mock target that would return a different version
        mock_target = MagicMock()
        mock_target.read_version.return_value = "0.1.0"  # different from releasable
        mock_target.tag_format.return_value = "v1.5.0"

        from rlsbl.commands.release.validate import compute_release_version

        with patch("rlsbl.commands.release.run") as mock_run:
            # No existing tag
            mock_run.return_value = ""

            current, new, bump, tag = compute_release_version(
                mock_target, str(tmp_path), None,
                None, None, lambda msg: None,
                workspace_root=str(tmp_path),
                releasable_name="core",
            )

        assert current == "1.5.0"
        assert new == "1.5.0"  # first release: version as-is
        # The target's read_version should NOT have been called
        mock_target.read_version.assert_not_called()

    def test_no_releasable_reads_from_target(self, tmp_path):
        """Without workspace_root/releasable_name, reads from target as before."""
        _init_git(tmp_path)

        mock_target = MagicMock()
        mock_target.read_version.return_value = "0.3.0"
        mock_target.tag_format.return_value = "v0.3.0"

        from rlsbl.commands.release.validate import compute_release_version

        with patch("rlsbl.commands.release.run") as mock_run:
            mock_run.return_value = ""

            current, new, bump, tag = compute_release_version(
                mock_target, str(tmp_path), None,
                None, None, lambda msg: None,
            )

        assert current == "0.3.0"
        mock_target.read_version.assert_called_once_with(str(tmp_path))

    def test_releasable_version_with_bump(self, tmp_path):
        """Releasable version is bumped correctly."""
        _init_git(tmp_path)

        write_releasable_version(str(tmp_path), "core", "1.0.0")

        mock_target = MagicMock()
        mock_target.tag_format.side_effect = lambda v: f"v{v}"

        from rlsbl.commands.release.validate import compute_release_version

        with patch("rlsbl.commands.release.run") as mock_run:
            # First call: tag exists (current tag check)
            # Second call: new tag doesn't exist
            mock_run.side_effect = ["v1.0.0\n", ""]

            current, new, bump, tag = compute_release_version(
                mock_target, str(tmp_path), "minor",
                None, None, lambda msg: None,
                workspace_root=str(tmp_path),
                releasable_name="core",
            )

        assert current == "1.0.0"
        assert new == "1.1.0"
        assert bump == "minor"
        assert tag == "v1.1.0"

    def test_missing_releasable_version_raises(self, tmp_path):
        """If releasable version file is missing, WorkspaceError propagates."""
        _init_git(tmp_path)

        mock_target = MagicMock()

        from rlsbl.commands.release.validate import compute_release_version

        with pytest.raises(WorkspaceError, match="version file missing"):
            compute_release_version(
                mock_target, str(tmp_path), None,
                None, None, lambda msg: None,
                workspace_root=str(tmp_path),
                releasable_name="nonexistent",
            )


# ---------------------------------------------------------------------------
# version-consistency check: explicit vs implicit mode
# ---------------------------------------------------------------------------


class TestVersionConsistencyExplicitMode:
    """version-consistency check in explicit releasable mode."""

    def _get_check_fn(self):
        """Register checks and return the version-consistency function."""
        mock_app = MagicMock()
        mock_app._checks_enabled = True
        registered = {}

        def capture_check(name):
            def decorator(fn):
                registered[name] = fn
                return fn
            return decorator

        mock_app.check = capture_check

        from rlsbl.checks.project import register_project_checks
        register_project_checks(mock_app)
        return registered["version-consistency"]

    def test_explicit_mode_uses_releasable_version(self, tmp_path):
        """In explicit mode, the check returns the releasable version."""
        # Set up workspace in explicit mode
        _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")

        # Write releasable version
        write_releasable_version(str(tmp_path), "core", "2.0.0")

        # Create project directory with a DIFFERENT version in its manifest
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n'
        )

        # Build check context for the project directory
        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        # Should pass with the releasable version, not fail on mismatch.
        # Without a per-package config, the member defaults to private,
        # so only the version file is checked.
        assert result.status == "pass"
        assert "2.0.0" in result.message
        assert "version file" in result.message

    def test_no_releasables_compares_targets(self, tmp_path):
        """Without [[releasables]], the check compares target versions."""
        _write_workspace(tmp_path, """\
[[projects]]
path = "pkg"
name = "pkg"
""")

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.5.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        assert result.status == "pass"
        assert "0.5.0" in result.message
        assert "1 target(s)" in result.message

    def test_explicit_mode_missing_version_file_falls_through(self, tmp_path):
        """If releasable version file is missing, falls through to target check."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        # Do NOT write a releasable version file

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.3.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        # Should fall through to the standard check
        assert result.status == "pass"
        assert "0.3.0" in result.message
        assert "1 target(s)" in result.message

    def test_non_monorepo_project_uses_target_check(self, tmp_path):
        """Non-monorepo projects use the standard per-target check."""
        pkg_dir = tmp_path
        (pkg_dir / "pyproject.toml").write_text(
            '[project]\nname = "standalone"\nversion = "1.0.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=pkg_dir,
            workspace_root=None,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        assert result.status == "pass"
        assert "1.0.0" in result.message

    def test_releasable_false_project_uses_target_check(self, tmp_path):
        """A project with releasable=false uses the standard per-target check."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "lib"
name = "lib"
releasable = "core"

[[projects]]
path = "tool"
name = "tool"
releasable = false
""")

        write_releasable_version(str(tmp_path), "core", "2.0.0")

        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "pyproject.toml").write_text(
            '[project]\nname = "tool"\nversion = "0.1.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(
            project_root=tool_dir,
            workspace_root=tmp_path,
            config={},
        )

        check_fn = self._get_check_fn()
        result = check_fn(ctx)

        # Should use the target version, not the releasable version
        assert result.status == "pass"
        assert "0.1.0" in result.message
        assert "1 target(s)" in result.message


# ---------------------------------------------------------------------------
# resolve_monorepo_context returns releasable_name
# ---------------------------------------------------------------------------


class TestResolveMonorepoContextReleasableName:
    """resolve_monorepo_context returns releasable_name in explicit mode."""

    def test_explicit_mode_returns_releasable_name(self, tmp_path):
        """In explicit mode, the fifth return value is the releasable name."""
        _write_workspace(tmp_path, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        from rlsbl.commands.release.validate import resolve_monorepo_context
        name, path, is_lib, is_non_releasable, rel_name = resolve_monorepo_context(
            str(tmp_path), pkg_dir, lambda msg: None
        )
        assert name == "pkg"
        assert rel_name == "core"

    def test_no_releasables_returns_none(self, tmp_path):
        """Without [[releasables]], releasable_name is None."""
        _write_workspace(tmp_path, """\
[[projects]]
path = "pkg"
name = "pkg"
""")
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        from rlsbl.commands.release.validate import resolve_monorepo_context
        name, path, is_lib, is_non_releasable, rel_name = resolve_monorepo_context(
            str(tmp_path), pkg_dir, lambda msg: None
        )
        assert name == "pkg"
        assert rel_name is None

    def test_not_monorepo_returns_none(self, tmp_path):
        """When not in a monorepo, releasable_name is None."""
        from rlsbl.commands.release.validate import resolve_monorepo_context
        name, path, is_lib, is_non_releasable, rel_name = resolve_monorepo_context(
            None, tmp_path, lambda msg: None
        )
        assert name is None
        assert rel_name is None
