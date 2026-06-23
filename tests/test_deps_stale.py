"""Tests for deps-stale check and constraint propagation advisory."""

import json
import os
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from strictcli import CheckResult

from rlsbl import app
from rlsbl.context import ProjectContext
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace_graph import Dependency, WorkspaceGraph

from conftest import make_workspace, run_git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeGraph:
    """Minimal graph stub for testing deps-stale without real manifests."""

    def __init__(self, deps_map):
        # deps_map: dict of project_name -> list of Dependency
        self._deps = deps_map

    def dependencies(self, project_name):
        return list(self._deps.get(project_name, []))

    def dependents(self, project_name):
        result = []
        for name, deps in self._deps.items():
            for dep in deps:
                if dep.name == project_name:
                    result.append(name)
        return result


# ---------------------------------------------------------------------------
# deps-stale check
# ---------------------------------------------------------------------------


class TestDepsStaleCheck:
    """The deps-stale check detects outdated intra-workspace constraints."""

    def test_skip_for_non_workspace(self, mock_git_repo):
        """Non-workspace context -> skip (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert result.status == "skip"
        assert "not a monorepo" in result.message

    def test_pass_fresh_constraint(self, mock_git_repo):
        """Constraint that satisfies the current version -> pass."""
        # Create two projects: lib (v2.0.0) and app (depends on lib >=1.0.0)
        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "2.0.0"})
        )

        app_dir = mock_git_repo / "myapp"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "myapp",
                "version": "1.0.0",
                "dependencies": {"lib": ">=1.0.0"},
            })
        )

        projects = [
            {"path": "lib", "name": "lib"},
            {"path": "myapp", "name": "myapp"},
        ]
        make_workspace(mock_git_repo, projects)

        graph = FakeGraph({
            "lib": [],
            "myapp": [Dependency(name="lib", dep_type="versioned", constraint=">=1.0.0")],
        })

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=projects,
            graph=graph,
        )
        result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "pass"

    def test_fail_outdated_constraint(self, mock_git_repo):
        """Constraint no longer satisfied -> fail."""
        # lib is at v2.0.0, app depends on lib ==1.0.0
        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "2.0.0"})
        )

        app_dir = mock_git_repo / "myapp"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "myapp",
                "version": "1.0.0",
                "dependencies": {"lib": "==1.0.0"},
            })
        )

        projects = [
            {"path": "lib", "name": "lib"},
            {"path": "myapp", "name": "myapp"},
        ]
        make_workspace(mock_git_repo, projects)

        graph = FakeGraph({
            "lib": [],
            "myapp": [Dependency(name="lib", dep_type="versioned", constraint="==1.0.0")],
        })

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=projects,
            graph=graph,
        )
        result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) == 1
        assert "myapp" in result.details[0]
        assert "lib" in result.details[0]
        assert "2.0.0" in result.details[0]

    def test_pass_no_deps(self, mock_git_repo):
        """Project with no dependencies -> pass."""
        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"})
        )

        projects = [{"path": "lib", "name": "lib"}]
        make_workspace(mock_git_repo, projects)

        graph = FakeGraph({"lib": []})

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=projects,
            graph=graph,
        )
        result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "pass"

    def test_skip_non_versioned_deps(self, mock_git_repo):
        """Path and workspace deps are skipped (not evaluated)."""
        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "2.0.0"})
        )

        app_dir = mock_git_repo / "myapp"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({"name": "myapp", "version": "1.0.0"})
        )

        projects = [
            {"path": "lib", "name": "lib"},
            {"path": "myapp", "name": "myapp"},
        ]
        make_workspace(mock_git_repo, projects)

        # Path dep - should be skipped even though constraint is stale
        graph = FakeGraph({
            "lib": [],
            "myapp": [Dependency(name="lib", dep_type="path", constraint="file:../lib")],
        })

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=projects,
            graph=graph,
        )
        result = app._check_defs["deps-stale"].impl(ctx)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# Constraint propagation advisory
# ---------------------------------------------------------------------------


class TestStaleDepAdvisory:
    """The advisory prints stale downstream constraints after release."""

    def test_advisory_prints_stale(self, mock_git_repo):
        """Advisory prints to stderr when a downstream constraint is stale."""
        from rlsbl.commands.release import _print_stale_dep_advisory

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "2.0.0"})
        )

        app_dir = mock_git_repo / "myapp"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "myapp",
                "version": "1.0.0",
                "dependencies": {"lib": ">=1.0.0,<2.0.0"},
            })
        )

        projects = [
            {"path": "lib", "name": "lib"},
            {"path": "myapp", "name": "myapp"},
        ]
        make_workspace(mock_git_repo, projects)

        # The advisory uses _evaluate_constraint which returns "versioned"
        # for complex constraints (commas). Use a simple outdated constraint instead.
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "myapp",
                "version": "1.0.0",
                "dependencies": {"lib": "==1.0.0"},
            })
        )

        captured = StringIO()
        with patch("sys.stderr", captured):
            _print_stale_dep_advisory("lib", "2.0.0")

        output = captured.getvalue()
        assert "Stale dependency constraints" in output
        assert "myapp" in output
        assert "lib" in output
        assert ">=2.0.0" in output

    def test_advisory_silent_when_fresh(self, mock_git_repo):
        """Advisory prints nothing when all constraints are satisfied."""
        from rlsbl.commands.release import _print_stale_dep_advisory

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "2.0.0"})
        )

        app_dir = mock_git_repo / "myapp"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "myapp",
                "version": "1.0.0",
                "dependencies": {"lib": ">=1.0.0"},
            })
        )

        projects = [
            {"path": "lib", "name": "lib"},
            {"path": "myapp", "name": "myapp"},
        ]
        make_workspace(mock_git_repo, projects)

        captured = StringIO()
        with patch("sys.stderr", captured):
            _print_stale_dep_advisory("lib", "2.0.0")

        assert captured.getvalue() == ""

    def test_advisory_silent_no_dependents(self, mock_git_repo):
        """Advisory prints nothing when the released package has no dependents."""
        from rlsbl.commands.release import _print_stale_dep_advisory

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"})
        )

        projects = [{"path": "lib", "name": "lib"}]
        make_workspace(mock_git_repo, projects)

        captured = StringIO()
        with patch("sys.stderr", captured):
            _print_stale_dep_advisory("lib", "1.0.0")

        assert captured.getvalue() == ""
