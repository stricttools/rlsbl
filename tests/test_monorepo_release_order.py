"""Tests for monorepo release-order subcommand."""

import json
import os

import pytest

from rlsbl.commands.monorepo import _cmd_release_order, _cmd_init
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


class TestReleaseOrderWithDeps:
    """Test release-order with dependency chains."""

    def test_linear_chain(self, mock_git_repo, capsys):
        """A -> B -> C should show C, B, A."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_release_order({}, project_root=".")
        captured = capsys.readouterr()

        assert "Release order (leaves first):" in captured.out
        lines = [l.strip() for l in captured.out.strip().split("\n") if l.strip() and l.strip()[0].isdigit()]
        assert len(lines) == 3
        assert lines[0] == "1. C"
        assert lines[1] == "2. B"
        assert lines[2] == "3. A"

    def test_diamond(self, mock_git_repo, capsys):
        """A -> B, A -> C, B -> D, C -> D: D first, A last."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0", "C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"D": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C", deps={"D": "^1.0.0"})
        _make_npm_project(mock_git_repo, "D")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
            {"path": "D", "name": "D"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_release_order({}, project_root=".")
        captured = capsys.readouterr()

        lines = [l.strip() for l in captured.out.strip().split("\n") if l.strip() and l.strip()[0].isdigit()]
        names = [l.split(". ", 1)[1] for l in lines]
        assert names[0] == "D"
        assert names[-1] == "A"
        # B and C must come after D but before A
        assert names.index("B") > names.index("D")
        assert names.index("C") > names.index("D")
        assert names.index("B") < names.index("A")
        assert names.index("C") < names.index("A")


class TestReleaseOrderIndependent:
    """Test release-order with no dependencies."""

    def test_independent_projects(self, mock_git_repo, capsys):
        """Projects with no deps are shown alphabetically with independence note."""
        _make_npm_project(mock_git_repo, "zeta")
        _make_npm_project(mock_git_repo, "alpha")
        _make_npm_project(mock_git_repo, "mid")

        projects = [
            {"path": "zeta", "name": "zeta"},
            {"path": "alpha", "name": "alpha"},
            {"path": "mid", "name": "mid"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_release_order({}, project_root=".")
        captured = capsys.readouterr()

        assert "All projects are independent (no intra-workspace dependencies)." in captured.out
        # Should NOT contain numbered items
        assert "1." not in captured.out
        # Names should appear in alphabetical order
        lines = [l.strip() for l in captured.out.strip().split("\n") if l.strip() and not l.strip().startswith("All")]
        assert lines == ["alpha", "mid", "zeta"]


class TestReleaseOrderCycle:
    """Test release-order with cyclic dependencies."""

    def test_cycle_error(self, mock_git_repo, capsys):
        """Cycle in dependencies should print error and exit 1."""
        _make_npm_project(mock_git_repo, "X", deps={"Y": "^1.0.0"})
        _make_npm_project(mock_git_repo, "Y", deps={"X": "^1.0.0"})

        projects = [
            {"path": "X", "name": "X"},
            {"path": "Y", "name": "Y"},
        ]
        _init_workspace(mock_git_repo, projects)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_release_order({}, project_root=".")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "cycle" in captured.err.lower()


class TestReleaseOrderEdgeCases:
    """Edge cases for release-order."""

    def test_empty_workspace(self, mock_git_repo, capsys):
        """Empty workspace prints a message and returns."""
        _cmd_init({}, project_root=".")
        capsys.readouterr()

        _cmd_release_order({}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." in captured.out

    def test_no_workspace(self, mock_git_repo):
        """No workspace should error and exit 1."""
        with pytest.raises(SystemExit):
            _cmd_release_order({}, project_root=".")
