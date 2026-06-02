"""Tests for the changelog-exempt boundary guardrail check.

The changelog-exempt-boundary check ensures that non-exempt projects do not
have runtime dependencies on changelog-exempt projects (since bug fixes
in exempt projects would silently bypass changelog coverage).
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import make_workspace, run_git
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.checks import register_checks
from rlsbl.workspace import load_workspace, WORKSPACE_DIR
from rlsbl.workspace_graph import WorkspaceGraph


def _register_and_get_checks():
    """Register all checks on a mock app and return the check function dict."""
    mock_app = MagicMock()
    mock_app._checks_enabled = True
    registered = {}

    def fake_check(name):
        def decorator(func):
            registered[name] = func
            return func
        return decorator

    mock_app.check = fake_check
    register_checks(mock_app)
    return registered


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
      path, name, changelog_exempt (optional bool),
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
        if spec.get("changelog_exempt"):
            ws_proj["changelog_exempt"] = True
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
    """Tests for check_changelog_exempt_boundary."""

    def test_boundary_catches_runtime_dependency(self, tmp_path, monkeypatch):
        """Non-exempt A depends on exempt B via runtime dep -> FAIL."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "changelog_exempt": True},
        ])
        checks = _register_and_get_checks()
        result = checks["changelog-exempt-boundary"](ctx)

        assert result.status == "fail"
        assert "proj-a" in result.details[0]
        assert "proj-b" in result.details[0]

    def test_boundary_allows_dev_dependency(self, tmp_path, monkeypatch):
        """Non-exempt A depends on exempt B via dev dep only -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "dev_deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "changelog_exempt": True},
        ])
        checks = _register_and_get_checks()
        result = checks["changelog-exempt-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_allows_no_dependents(self, tmp_path, monkeypatch):
        """Exempt B with no dependents at all -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a"},
            {"path": "proj-b", "name": "proj-b", "changelog_exempt": True},
        ])
        checks = _register_and_get_checks()
        result = checks["changelog-exempt-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_transitive_chain(self, tmp_path, monkeypatch):
        """A->B->C where C is exempt, A is not, all runtime -> FAIL.

        A transitively depends on exempt C through non-exempt B.
        """
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "deps": ["proj-c"]},
            {"path": "proj-c", "name": "proj-c", "changelog_exempt": True},
        ])
        checks = _register_and_get_checks()
        result = checks["changelog-exempt-boundary"](ctx)

        assert result.status == "fail"
        # Should mention proj-a (the non-exempt project that transitively
        # depends on exempt proj-c)
        violation_text = "\n".join(result.details)
        assert "proj-a" in violation_text
        assert "proj-c" in violation_text
