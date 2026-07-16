"""Tests for the dev node boundary guardrail check.

The dev-node-boundary check ensures that non-dev-node projects do not
have runtime dependencies on dev node projects (since bug fixes
in dev node projects would silently bypass changelog coverage).
"""

import json
import os
from pathlib import Path

import pytest

from conftest import capture_all_checks, make_workspace, run_git
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace import load_workspace, WORKSPACE_DIR
from rlsbl.workspace_graph import WorkspaceGraph


def _register_and_get_checks():
    return capture_all_checks()


def _make_pypi_project(root, subdir, version="0.1.0", deps=None, dev_deps=None):
    """Create a minimal pyproject.toml with optional runtime and dev deps."""
    proj_dir = root / subdir
    proj_dir.mkdir(parents=True, exist_ok=True)
    dep_lines = ""
    if deps:
        items = ", ".join(f'"{d}"' for d in deps)
        dep_lines = f"dependencies = [{items}]"
    dev_lines = ""
    if dev_deps:
        items = ", ".join(f'"{d}"' for d in dev_deps)
        dev_lines = f'[project.optional-dependencies]\ndev = [{items}]'
    content = (
        f'[project]\nname = "{subdir}"\nversion = "{version}"\n'
        f'{dep_lines}\n\n{dev_lines}\n'
    )
    (proj_dir / "pyproject.toml").write_text(content)


def _setup_monorepo(tmp_path, monkeypatch, projects_spec):
    """Set up a monorepo with given projects and return (root, ctx).

    projects_spec is a list of dicts with keys:
      path, name, dev_node (optional bool),
      deps (optional list of runtime dep names),
      dev_deps (optional list of dev dep names).
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# monorepo\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    ws_projects = []
    for spec in projects_spec:
        ws_proj = {"path": spec["path"], "name": spec["name"]}
        if spec.get("dev_node"):
            ws_proj["dev_node"] = True
        ws_projects.append(ws_proj)

    make_workspace(tmp_path, ws_projects)

    # Create pyproject.toml files for each project
    for spec in projects_spec:
        _make_pypi_project(
            tmp_path,
            spec["path"],
            deps=spec.get("deps"),
            dev_deps=spec.get("dev_deps"),
        )

    # Commit everything
    run_git(tmp_path, "add", WORKSPACE_DIR)
    for spec in projects_spec:
        run_git(tmp_path, "add", spec["path"])
    run_git(tmp_path, "commit", "-q", "-m", "add projects")

    # Load workspace and build graph
    projects = load_workspace(str(tmp_path))
    graph = WorkspaceGraph(str(tmp_path), projects)

    # Create a WorkspaceCheckContext (project_root doesn't matter for this check,
    # it operates on all projects via ctx.projects and ctx.graph)
    ctx = WorkspaceCheckContext(
        project_root=tmp_path,
        workspace_root=tmp_path,
        config={},
        projects=projects,
        graph=graph,
    )
    return tmp_path, ctx


class TestBoundaryGuardrail:
    """Tests for check_dev_node_boundary."""

    def test_boundary_catches_runtime_dependency(self, tmp_path, monkeypatch):
        """Non-dev-node A depends on dev node B via runtime dep -> FAIL."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "dev_node": True},
        ])
        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        assert result.status == "fail"
        assert "proj-a" in result.problems[0].text
        assert "proj-b" in result.problems[0].text

    def test_boundary_allows_dev_dependency(self, tmp_path, monkeypatch):
        """Non-dev-node A depends on dev node B via dev dep only -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "dev_deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "dev_node": True},
        ])
        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_allows_no_dependents(self, tmp_path, monkeypatch):
        """Dev node B with no dependents at all -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a"},
            {"path": "proj-b", "name": "proj-b", "dev_node": True},
        ])
        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_transitive_chain(self, tmp_path, monkeypatch):
        """A->B->C where C is dev node, A is not, all runtime -> FAIL.

        A transitively depends on dev node C through non-dev-node B.
        """
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "deps": ["proj-c"]},
            {"path": "proj-c", "name": "proj-c", "dev_node": True},
        ])
        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        assert result.status == "fail"
        # Should mention proj-a (the non-dev-node project that transitively
        # depends on dev node proj-c)
        violation_text = "\n".join(p.text for p in result.problems)
        assert "proj-a" in violation_text
        assert "proj-c" in violation_text
