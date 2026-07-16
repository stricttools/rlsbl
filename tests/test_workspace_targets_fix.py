"""Tests for workspace-targets check filtering and releasable union verification.

Phase 6: verifies that the workspace-targets check correctly:
- Skips dev_only projects (don't cause failure even with no targets)
- Skips releasable=false projects (explicitly non-releasable)
- Passes releasable members that inherit targets
- Fails at the union check when a releasable has no aggregate targets
- Passes when at least one member in a releasable has targets
- Reports which releasable has no targets in the failure message
- Shows correct counts in the pass message
"""

import os
from pathlib import Path

import pytest

from conftest import run_git

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.checks.scope import scope_adapter
from rlsbl.workspace import Releasable, WorkspaceProject

from strictcli import SkipCheck


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


class _SkipOutcome:
    """Wrapper for SkipCheck with .status/.message for uniform test assertions."""
    __slots__ = ("status", "message", "problems")

    def __init__(self, skip: SkipCheck):
        self.status = "skip"
        self.message = skip.reason
        self.problems = ()


def _init_repo(repo):
    """Initialize a minimal git repo with one commit."""
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")


def _make_ws_ctx(repo, projects, releasables=None):
    """Create a WorkspaceCheckContext for workspace-targets tests."""
    return WorkspaceCheckContext(
        project_root=Path(str(repo)),
        workspace_root=Path(str(repo)),
        config={},
        projects=projects,
        graph=None,
        releasables=releasables or [],
    )


def _run_check(name, ctx):
    """Run a check through the scope adapter."""
    cdef = app._check_defs[name]
    check_ctx = ctx
    if cdef.scope:
        adapted = scope_adapter(ctx, cdef.scope)
        if isinstance(adapted, SkipCheck):
            return _SkipOutcome(adapted)
        check_ctx = adapted
    return cdef.impl(check_ctx)


def _make_pypi_project(repo, name):
    """Create a directory with a pyproject.toml so detect_targets finds pypi."""
    pkg = repo / name
    pkg.mkdir(exist_ok=True)
    (pkg / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
    )


def _make_empty_project(repo, name):
    """Create a project directory with no target manifests."""
    pkg = repo / name
    pkg.mkdir(exist_ok=True)
    # Just a README -- no pyproject.toml, package.json, or go.mod
    (pkg / "README.md").write_text(f"# {name}\n")


# ==================================================================
# Tests
# ==================================================================


class TestDevOnlySkipped:
    """dev_only projects are skipped and don't cause failure even with no targets."""

    def test_dev_only_no_targets_does_not_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # A dev_only project with no targets
        _make_empty_project(repo, "devtool")

        projects = [
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_only": True}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"

    def test_dev_node_no_targets_does_not_fail(self, tmp_path, monkeypatch):
        """dev_node implies dev_only, so it should also be skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_empty_project(repo, "conformance")

        projects = [
            WorkspaceProject({"name": "conformance", "path": "conformance", "dev_node": True}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"


class TestReleasableFalseSkipped:
    """releasable=false projects are skipped."""

    def test_releasable_false_no_targets_does_not_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_empty_project(repo, "docs-only")

        projects = [
            WorkspaceProject({"name": "docs-only", "path": "docs-only", "releasable": False}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"

    def test_releasable_false_mixed_with_normal(self, tmp_path, monkeypatch):
        """A normal project with targets + releasable=false project without targets passes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "core")
        _make_empty_project(repo, "docs-site")

        projects = [
            WorkspaceProject({"name": "core", "path": "core"}),
            WorkspaceProject({"name": "docs-site", "path": "docs-site", "releasable": False}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "1 project(s)" in result.message
        assert "1 skipped" in result.message


class TestReleasableMembersInheritTargets:
    """Releasable members with inherited targets pass."""

    def test_member_with_own_targets_passes(self, tmp_path, monkeypatch):
        """A releasable member that has its own targets passes the per-project check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "core")

        projects = [
            WorkspaceProject({"name": "core", "path": "core", "releasable": "main"}),
        ]
        releasables = [Releasable(name="main")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "1 releasable(s) verified" in result.message


class TestReleasableUnionCheckFails:
    """A releasable with no aggregate targets across all members fails."""

    def test_releasable_zero_union_targets_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # Two members, both with no targets
        _make_empty_project(repo, "pkg-a")
        _make_empty_project(repo, "pkg-b")

        projects = [
            WorkspaceProject({"name": "pkg-a", "path": "pkg-a", "releasable": "core"}),
            WorkspaceProject({"name": "pkg-b", "path": "pkg-b", "releasable": "core"}),
        ]
        releasables = [Releasable(name="core")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "fail"
        assert "core" in result.message
        # Both per-project failures and releasable failure should be reported
        assert any("releasable 'core'" in p.text for p in result.problems)

    def test_failure_reports_releasable_name(self, tmp_path, monkeypatch):
        """The failure message explicitly names which releasable has no targets."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_empty_project(repo, "widget")

        projects = [
            WorkspaceProject({"name": "widget", "path": "widget", "releasable": "gadget"}),
        ]
        releasables = [Releasable(name="gadget")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "fail"
        assert "gadget" in result.message


class TestReleasableUnionCheckPasses:
    """A releasable with at least one member having targets passes."""

    def test_one_member_has_targets(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        # One member with targets, one without
        _make_pypi_project(repo, "core-lib")
        _make_empty_project(repo, "core-utils")

        projects = [
            WorkspaceProject({"name": "core-lib", "path": "core-lib", "releasable": "core"}),
            WorkspaceProject({"name": "core-utils", "path": "core-utils", "releasable": "core"}),
        ]
        releasables = [Releasable(name="core")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        # Per-project: core-utils fails. But releasable union passes (core-lib has pypi).
        # The check should still fail because core-utils itself has no targets
        assert result.status == "fail"
        assert "core-utils" in result.message
        # But the releasable union check should NOT appear in details
        assert not any("releasable 'core'" in p.text for p in result.problems)

    def test_all_members_have_targets(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "lib-a")
        _make_pypi_project(repo, "lib-b")

        projects = [
            WorkspaceProject({"name": "lib-a", "path": "lib-a", "releasable": "suite"}),
            WorkspaceProject({"name": "lib-b", "path": "lib-b", "releasable": "suite"}),
        ]
        releasables = [Releasable(name="suite")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "1 releasable(s) verified" in result.message


class TestPassMessageCounts:
    """The pass message shows correct counts."""

    def test_no_skipped_no_releasables(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "alpha")
        _make_pypi_project(repo, "beta")

        projects = [
            WorkspaceProject({"name": "alpha", "path": "alpha"}),
            WorkspaceProject({"name": "beta", "path": "beta"}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "all 2 project(s) have targets" in result.message
        # No skipped, no releasables
        assert "skipped" not in result.message
        assert "releasable" not in result.message

    def test_with_skipped_projects(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "app")
        _make_empty_project(repo, "devtool")
        _make_empty_project(repo, "internal")

        projects = [
            WorkspaceProject({"name": "app", "path": "app"}),
            WorkspaceProject({"name": "devtool", "path": "devtool", "dev_only": True}),
            WorkspaceProject({"name": "internal", "path": "internal", "releasable": False}),
        ]
        ctx = _make_ws_ctx(repo, projects)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "all 1 project(s) have targets" in result.message
        assert "2 skipped" in result.message

    def test_with_releasables(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "pkg-x")
        _make_pypi_project(repo, "pkg-y")

        projects = [
            WorkspaceProject({"name": "pkg-x", "path": "pkg-x", "releasable": "rel1"}),
            WorkspaceProject({"name": "pkg-y", "path": "pkg-y", "releasable": "rel2"}),
        ]
        releasables = [Releasable(name="rel1"), Releasable(name="rel2")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "all 2 project(s) have targets" in result.message
        assert "2 releasable(s) verified" in result.message

    def test_with_skipped_and_releasables(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _init_repo(repo)

        _make_pypi_project(repo, "lib")
        _make_empty_project(repo, "testutils")

        projects = [
            WorkspaceProject({"name": "lib", "path": "lib", "releasable": "main"}),
            WorkspaceProject({"name": "testutils", "path": "testutils", "dev_only": True}),
        ]
        releasables = [Releasable(name="main")]
        ctx = _make_ws_ctx(repo, projects, releasables=releasables)
        result = _run_check("workspace-targets", ctx)
        assert result.status == "pass"
        assert "all 1 project(s) have targets" in result.message
        assert "1 skipped" in result.message
        assert "1 releasable(s) verified" in result.message
