"""Tests for monorepo batch release: file reading, validation, ordering, and finalization."""

import json
import os
import stat
import time
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.release_file import (
    BatchReleaseConfig,
    ReleaseConfig,
    get_batch_release_file_path,
    read_batch_release_file,
)
from rlsbl.commands.monorepo.batch_release import _cmd_batch_release, _finalize_batch_file
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


def _write_toml(path, content):
    """Write a TOML string to a file, creating directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_pypi_project(base_path, subdir, version="0.1.0", deps=None):
    """Create a minimal pypi project."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    dep_lines = ""
    if deps:
        dep_items = ", ".join(f'"{d}"' for d in deps)
        dep_lines = f"dependencies = [{dep_items}]"
    content = f'[project]\nname = "{subdir}"\nversion = "{version}"\n{dep_lines}\n'
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(content)


def _make_npm_project(base_path, subdir, version="0.1.0", deps=None):
    """Create a minimal npm project with optional dependencies."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    if deps:
        pkg["dependencies"] = deps
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)


def _init_workspace(base_path, projects):
    """Initialize a workspace with the given project list."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


# ---------------------------------------------------------------------------
# Batch file path
# ---------------------------------------------------------------------------


class TestGetBatchReleaseFilePath:

    def test_default_root(self):
        result = get_batch_release_file_path()
        assert result == os.path.join(".", ".rlsbl-monorepo", "releases", "unreleased.toml")

    def test_custom_root(self):
        result = get_batch_release_file_path("/my/workspace")
        assert result == os.path.join("/my/workspace", ".rlsbl-monorepo", "releases", "unreleased.toml")


# ---------------------------------------------------------------------------
# Batch file reading
# ---------------------------------------------------------------------------


class TestReadBatchReleaseFile:

    def test_multiple_packages_parsed(self, tmp_path):
        """Multiple packages in the batch file are parsed correctly."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.models]\n'
            'bump = "major"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
            '\n'
            '[packages."marketplace-contract"]\n'
            'bump = "minor"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
        )
        config = read_batch_release_file(str(batch_file))

        assert isinstance(config, BatchReleaseConfig)
        assert len(config.packages) == 2

        assert "models" in config.packages
        assert config.packages["models"].bump == "major"
        assert config.packages["models"].include == ["pypi"]
        assert config.packages["models"].exclude == []

        assert "marketplace-contract" in config.packages
        assert config.packages["marketplace-contract"].bump == "minor"
        assert config.packages["marketplace-contract"].include == ["pypi"]
        assert config.packages["marketplace-contract"].exclude == []

    def test_single_package(self, tmp_path):
        """A single package is valid."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
        )
        config = read_batch_release_file(str(batch_file))
        assert len(config.packages) == 1
        assert config.packages["mylib"].bump == "patch"

    def test_with_targets(self, tmp_path):
        """Per-target configuration is preserved."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.myapp]\n'
            'bump = "minor"\n'
            'include = ["flutter-ios"]\n'
            'exclude = []\n'
            '\n'
            '[packages.myapp.targets.flutter-ios]\n'
            'mode = "ota"\n'
        )
        config = read_batch_release_file(str(batch_file))
        assert config.packages["myapp"].targets == {"flutter-ios": {"mode": "ota"}}

    def test_missing_packages_section(self, tmp_path):
        """Missing [packages] section raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text('bump = "patch"\n')
        with pytest.raises(ValueError, match="missing required section"):
            read_batch_release_file(str(batch_file))

    def test_empty_packages(self, tmp_path):
        """Empty [packages] section raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text('[packages]\n')
        with pytest.raises(ValueError, match="is empty"):
            read_batch_release_file(str(batch_file))

    def test_missing_bump(self, tmp_path):
        """Missing bump field in a package raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
        )
        with pytest.raises(ValueError, match=r"\[packages\.mylib\].*bump"):
            read_batch_release_file(str(batch_file))

    def test_invalid_bump(self, tmp_path):
        """Invalid bump value raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'bump = "huge"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
        )
        with pytest.raises(ValueError, match="invalid bump"):
            read_batch_release_file(str(batch_file))

    def test_missing_include(self, tmp_path):
        """Missing include field raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'bump = "patch"\n'
            'exclude = []\n'
        )
        with pytest.raises(ValueError, match="include"):
            read_batch_release_file(str(batch_file))

    def test_missing_exclude(self, tmp_path):
        """Missing exclude field raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'bump = "patch"\n'
            'include = ["pypi"]\n'
        )
        with pytest.raises(ValueError, match="exclude"):
            read_batch_release_file(str(batch_file))

    def test_include_exclude_overlap(self, tmp_path):
        """Overlap between include and exclude raises ValueError."""
        batch_file = tmp_path / "unreleased.toml"
        batch_file.write_text(
            '[packages.mylib]\n'
            'bump = "patch"\n'
            'include = ["pypi", "npm"]\n'
            'exclude = ["pypi"]\n'
        )
        with pytest.raises(ValueError, match="both include and exclude"):
            read_batch_release_file(str(batch_file))

    def test_file_not_found(self, tmp_path):
        """FileNotFoundError when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            read_batch_release_file(str(tmp_path / "nonexistent.toml"))


# ---------------------------------------------------------------------------
# Batch validation: package not in workspace
# ---------------------------------------------------------------------------


class TestBatchValidation:

    def test_package_not_in_workspace(self, mock_git_repo, capsys):
        """Package listed in batch file but not in workspace triggers error."""
        _make_npm_project(mock_git_repo, "alpha")
        projects = [{"path": "alpha", "name": "alpha"}]
        _init_workspace(mock_git_repo, projects)

        # Write batch file with a package not in workspace
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(
            batch_path,
            '[packages.alpha]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
            '\n'
            '[packages.nonexistent]\n'
            'bump = "minor"\n'
            'include = ["pypi"]\n'
            'exclude = []\n',
        )

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release({"dry-run": False, "yes": True, "quiet": False})

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "nonexistent" in captured.err
        assert "not found in workspace" in captured.err

    def test_no_batch_file(self, mock_git_repo, capsys):
        """Missing batch file triggers error."""
        _init_workspace(mock_git_repo, [])

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release({"dry-run": False, "yes": True, "quiet": False})

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No batch release file found" in captured.err


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


class TestBatchTopologicalOrder:

    def test_leaves_released_first(self, mock_git_repo, capsys):
        """Packages are released in topological order: leaves first."""
        # A depends on B, B depends on C -> order: C, B, A
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(
            batch_path,
            '[packages.A]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
            '\n'
            '[packages.B]\n'
            'bump = "minor"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
            '\n'
            '[packages.C]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n',
        )

        # Track which project dirs run_cmd is called from
        release_order = []

        def mock_run_cmd(release_config, flags):
            release_order.append(os.path.basename(os.getcwd()))

        with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            with patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
                _cmd_batch_release({"dry-run": False, "yes": True, "quiet": False})

        # Verify release_order: C before B before A
        assert release_order == ["C", "B", "A"]

    def test_subset_ordering(self, mock_git_repo, capsys):
        """Batch file with a subset of workspace packages preserves topo order."""
        # A -> B -> C, but only releasing A and C
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(
            batch_path,
            '[packages.A]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
            '\n'
            '[packages.C]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n',
        )

        release_order = []

        def mock_run_cmd(release_config, flags):
            release_order.append(os.path.basename(os.getcwd()))

        with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            with patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
                _cmd_batch_release({"dry-run": False, "yes": True, "quiet": False})

        # C should come before A (C is a leaf)
        assert release_order == ["C", "A"]

    def test_independent_packages_alphabetical(self, mock_git_repo, capsys):
        """Independent packages (no deps) are released in deterministic order."""
        _make_npm_project(mock_git_repo, "zeta")
        _make_npm_project(mock_git_repo, "alpha")

        projects = [
            {"path": "zeta", "name": "zeta"},
            {"path": "alpha", "name": "alpha"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(
            batch_path,
            '[packages.zeta]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n'
            '\n'
            '[packages.alpha]\n'
            'bump = "patch"\n'
            'include = ["npm"]\n'
            'exclude = []\n',
        )

        release_order = []

        def mock_run_cmd(release_config, flags):
            release_order.append(os.path.basename(os.getcwd()))

        with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            with patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
                _cmd_batch_release({"dry-run": False, "yes": True, "quiet": False})

        # Topological sort is deterministic (alphabetical for ties)
        assert release_order == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Batch file finalization
# ---------------------------------------------------------------------------


class TestBatchFinalization:

    def test_file_renamed_and_locked(self, mock_git_repo):
        """Batch file is renamed to timestamped name and made read-only."""
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, '[packages.x]\nbump = "patch"\ninclude = ["pypi"]\nexclude = []\n')

        messages = []

        with patch("rlsbl.commands.monorepo.batch_release.commit_files"):
            _finalize_batch_file(batch_path, messages.append)

        # The original file should now be empty (recreated)
        assert os.path.exists(batch_path)
        with open(batch_path) as f:
            assert f.read() == ""

        # A timestamped file should exist
        releases_dir = os.path.dirname(batch_path)
        batch_files = [
            f for f in os.listdir(releases_dir) if f.startswith("batch-")
        ]
        assert len(batch_files) == 1

        versioned_path = os.path.join(releases_dir, batch_files[0])
        mode = os.stat(versioned_path).st_mode
        assert mode & stat.S_IRUSR  # owner can read
        assert not (mode & stat.S_IWUSR)  # owner cannot write
        assert not (mode & stat.S_IWGRP)  # group cannot write
        assert not (mode & stat.S_IWOTH)  # others cannot write

    def test_finalize_commits_files(self, mock_git_repo):
        """Finalization calls commit_files with the correct paths."""
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, '[packages.x]\nbump = "patch"\ninclude = ["pypi"]\nexclude = []\n')

        committed_files = []

        def mock_commit(msg, files, allow_failure=False):
            committed_files.extend(files)

        with patch("rlsbl.commands.monorepo.batch_release.commit_files", mock_commit):
            _finalize_batch_file(batch_path, lambda msg: None)

        # Should commit both the versioned file and the new empty unreleased.toml
        assert len(committed_files) == 2
        assert any("batch-" in f for f in committed_files)
        assert any("unreleased.toml" in f for f in committed_files)

    def test_timestamp_format(self, mock_git_repo):
        """Versioned file name follows batch-YYYYMMDD-HHMMSS.toml format."""
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, '[packages.x]\nbump = "patch"\ninclude = ["pypi"]\nexclude = []\n')

        with patch("rlsbl.commands.monorepo.batch_release.commit_files"):
            _finalize_batch_file(batch_path, lambda msg: None)

        releases_dir = os.path.dirname(batch_path)
        batch_files = [f for f in os.listdir(releases_dir) if f.startswith("batch-")]
        assert len(batch_files) == 1

        name = batch_files[0]
        # Format: batch-YYYYMMDD-HHMMSS.toml
        assert name.startswith("batch-")
        assert name.endswith(".toml")
        parts = name[len("batch-"):-len(".toml")]
        date_part, time_part = parts.split("-", 1)
        assert len(date_part) == 8  # YYYYMMDD
        assert len(time_part) == 6  # HHMMSS
