"""Tests for monorepo outdated subcommand."""

import json
import os
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import (
    _cmd_outdated,
    _evaluate_constraint,
    _parse_version_tuple,
)
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


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


class TestOutdatedNoDeps:
    """Test outdated with no intra-workspace dependencies."""

    def test_no_deps(self, mock_git_repo, capsys):
        """Projects with no intra-workspace deps print a message."""
        _make_npm_project(mock_git_repo, "alpha")
        _make_npm_project(mock_git_repo, "beta")

        projects = [
            {"path": "alpha", "name": "alpha"},
            {"path": "beta", "name": "beta"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "No intra-workspace dependencies found." in captured.out

    def test_empty_workspace(self, mock_git_repo, capsys):
        """Empty workspace prints a message."""
        _init_workspace(mock_git_repo, [])

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." in captured.out

    def test_no_workspace(self, mock_git_repo):
        """No workspace should error and exit 1."""
        with pytest.raises(SystemExit):
            _cmd_outdated({}, project_root=".")


class TestOutdatedVersioned:
    """Test outdated with versioned dependencies."""

    def test_versioned_ok(self, mock_git_repo, capsys):
        """Versioned dep where current satisfies constraint shows 'ok'."""
        _make_npm_project(mock_git_repo, "api", deps={"core": ">=1.2.0"})
        _make_npm_project(mock_git_repo, "core", version="1.3.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "api" in captured.out
        assert "core" in captured.out
        assert ">=1.2.0" in captured.out
        assert "1.3.0" in captured.out
        assert "ok" in captured.out

    def test_versioned_outdated(self, mock_git_repo, capsys):
        """Versioned dep where current does not satisfy shows 'outdated'."""
        _make_npm_project(mock_git_repo, "api", deps={"core": ">=2.0.0"})
        _make_npm_project(mock_git_repo, "core", version="1.3.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "outdated" in captured.out

    def test_versioned_caret(self, mock_git_repo, capsys):
        """Caret constraint where current is in range shows 'ok'."""
        _make_npm_project(mock_git_repo, "api", deps={"core": "^1.2.0"})
        _make_npm_project(mock_git_repo, "core", version="1.5.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "ok" in captured.out

    def test_versioned_caret_outdated(self, mock_git_repo, capsys):
        """Caret constraint where current is below constraint shows 'outdated'."""
        _make_npm_project(mock_git_repo, "api", deps={"core": "^1.2.0"})
        _make_npm_project(mock_git_repo, "core", version="1.1.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "outdated" in captured.out


class TestOutdatedWorkspace:
    """Test outdated with workspace protocol dependencies."""

    def test_workspace_dep(self, mock_git_repo, capsys):
        """Workspace dep shows 'workspace' status."""
        _make_npm_project(mock_git_repo, "api", deps={"core": "workspace:*"})
        _make_npm_project(mock_git_repo, "core", version="0.5.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "workspace" in captured.out
        assert "workspace:*" in captured.out


class TestOutdatedPath:
    """Test outdated with path/file dependencies."""

    def test_path_dep(self, mock_git_repo, capsys):
        """File dep shows 'path' status."""
        _make_npm_project(mock_git_repo, "web", deps={"core": "file:../core"})
        _make_npm_project(mock_git_repo, "core", version="1.3.0")

        projects = [
            {"path": "web", "name": "web"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "path" in captured.out
        assert "file:../core" in captured.out


class TestOutdatedExplicit:
    """Test outdated with explicit (depends_on) dependencies."""

    def test_explicit_dep_status(self, mock_git_repo, capsys):
        """Explicit dep shows 'explicit' status and '(explicit)' constraint."""
        _make_npm_project(mock_git_repo, "api", version="1.0.0")
        _make_npm_project(mock_git_repo, "core", version="2.5.0")

        projects = [
            {"path": "api", "name": "api", "depends_on": ["core"]},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "explicit" in captured.out
        assert "(explicit)" in captured.out

    def test_explicit_dep_shows_current_version(self, mock_git_repo, capsys):
        """Explicit dep shows the dependency's current version."""
        _make_npm_project(mock_git_repo, "frontend", version="0.1.0")
        _make_npm_project(mock_git_repo, "backend", version="3.7.2")

        projects = [
            {"path": "frontend", "name": "frontend", "depends_on": ["backend"]},
            {"path": "backend", "name": "backend"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        assert "3.7.2" in captured.out
        assert "frontend" in captured.out
        assert "backend" in captured.out


class TestOutdatedMixed:
    """Test outdated with mixed dependency types."""

    def test_mixed_deps(self, mock_git_repo, capsys):
        """Table shows all three dep types."""
        _make_npm_project(mock_git_repo, "api", deps={
            "core": ">=1.2.0",
            "utils": "workspace:*",
        })
        _make_npm_project(mock_git_repo, "web", deps={
            "core": "file:../core",
        })
        _make_npm_project(mock_git_repo, "core", version="1.3.0")
        _make_npm_project(mock_git_repo, "utils", version="0.5.0")

        projects = [
            {"path": "api", "name": "api"},
            {"path": "web", "name": "web"},
            {"path": "core", "name": "core"},
            {"path": "utils", "name": "utils"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()

        # All three types should appear
        lines = captured.out.strip().split("\n")
        # Header + 3 data rows
        assert len(lines) == 4

        # Check header
        assert "Project" in lines[0]
        assert "Dependency" in lines[0]
        assert "Constraint" in lines[0]
        assert "Current" in lines[0]
        assert "Status" in lines[0]

        # Check each dep type is present somewhere
        full_output = captured.out
        assert "ok" in full_output
        assert "workspace" in full_output
        assert "path" in full_output


class TestOutdatedColumnAlignment:
    """Test column alignment in the outdated table."""

    def test_columns_aligned(self, mock_git_repo, capsys):
        """Columns should be aligned with consistent spacing."""
        _make_npm_project(mock_git_repo, "long-project-name", deps={
            "x": ">=0.1.0",
        })
        _make_npm_project(mock_git_repo, "x", version="0.1.0")

        projects = [
            {"path": "long-project-name", "name": "long-project-name"},
            {"path": "x", "name": "x"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_outdated({}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 2  # header + 1 row

        # All lines should have the same number of columns (split by 2+ spaces)
        header_parts = lines[0].split("  ")
        # Each column pair is separated by at least 2 spaces
        # Just verify the header and data row have the same structure
        assert "Project" in lines[0]
        assert "long-project-name" in lines[1]

        # The "Project" column should be padded to match "long-project-name"
        # since that is longer
        proj_col_width = lines[0].index("Dependency")
        data_col_width = lines[1].index("x")
        # Both the header and data rows should have the Dependency column
        # at the same position
        assert proj_col_width == data_col_width


class TestEvaluateConstraint:
    """Unit tests for _evaluate_constraint helper."""

    def test_gte_ok(self):
        assert _evaluate_constraint(">=1.2.0", "1.3.0") == "ok"

    def test_gte_exact(self):
        assert _evaluate_constraint(">=1.2.0", "1.2.0") == "ok"

    def test_gte_outdated(self):
        assert _evaluate_constraint(">=2.0.0", "1.9.9") == "outdated"

    def test_gt_ok(self):
        assert _evaluate_constraint(">1.0.0", "1.0.1") == "ok"

    def test_gt_exact_fail(self):
        assert _evaluate_constraint(">1.0.0", "1.0.0") == "outdated"

    def test_lte_ok(self):
        assert _evaluate_constraint("<=2.0.0", "1.5.0") == "ok"

    def test_lt_ok(self):
        assert _evaluate_constraint("<2.0.0", "1.9.9") == "ok"

    def test_eq_ok(self):
        assert _evaluate_constraint("==1.0.0", "1.0.0") == "ok"

    def test_eq_fail(self):
        assert _evaluate_constraint("==1.0.0", "1.0.1") == "outdated"

    def test_caret_same_major(self):
        assert _evaluate_constraint("^1.2.0", "1.5.0") == "ok"

    def test_caret_different_major(self):
        assert _evaluate_constraint("^1.2.0", "2.0.0") == "outdated"

    def test_caret_below(self):
        assert _evaluate_constraint("^1.2.0", "1.1.0") == "outdated"

    def test_tilde_same_minor(self):
        assert _evaluate_constraint("~1.2.0", "1.2.5") == "ok"

    def test_tilde_different_minor(self):
        assert _evaluate_constraint("~1.2.0", "1.3.0") == "outdated"

    def test_complex_constraint(self):
        assert _evaluate_constraint(">=1.0.0,<2.0.0", "1.5.0") == "versioned"

    def test_empty_constraint(self):
        assert _evaluate_constraint("", "1.0.0") == "versioned"

    def test_unparseable_version(self):
        assert _evaluate_constraint(">=1.0.0", "dev") == "versioned"

    def test_bare_version(self):
        assert _evaluate_constraint("1.0.0", "1.0.0") == "ok"

    def test_bare_version_mismatch(self):
        assert _evaluate_constraint("1.0.0", "1.0.1") == "outdated"


class TestParseVersionTuple:
    """Unit tests for _parse_version_tuple helper."""

    def test_simple(self):
        assert _parse_version_tuple("1.2.3") == (1, 2, 3)

    def test_two_parts(self):
        assert _parse_version_tuple("1.2") == (1, 2)

    def test_single(self):
        assert _parse_version_tuple("5") == (5,)

    def test_invalid(self):
        assert _parse_version_tuple("abc") is None

    def test_empty(self):
        assert _parse_version_tuple("") is None
