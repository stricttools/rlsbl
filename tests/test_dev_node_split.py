"""Tests for the dev_node split into dev_only + is_releasable.

Validates that the two independent concerns -- boundary guardrail (dev_only)
and release lifecycle membership (is_releasable) -- are correctly separated.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_commit, make_workspace, run_git
from rlsbl.workspace import WorkspaceProject, WORKSPACE_DIR


# ---------------------------------------------------------------------------
# WorkspaceProject property tests
# ---------------------------------------------------------------------------


class TestDevOnlyProperty:
    """dev_only property should reflect dev_only or legacy dev_node flag."""

    def test_dev_only_true(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True})
        assert proj.dev_only is True

    def test_dev_only_false(self):
        proj = WorkspaceProject({"name": "a", "path": "a"})
        assert proj.dev_only is False

    def test_legacy_dev_node_implies_dev_only(self):
        """Legacy dev_node = true should make dev_only True."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_node": True})
        assert proj.dev_only is True

    def test_dev_only_explicit_false(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": False})
        assert proj.dev_only is False


class TestDevNodeDerivedProperty:
    """dev_node is now derived: True when dev_only AND not a member of any releasable."""

    def test_legacy_dev_node_still_works(self):
        """Legacy workspace.toml with dev_node = true: dev_node property is True."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_node": True})
        assert proj.dev_node is True

    def test_dev_only_with_releasable_false(self):
        """dev_only + releasable = false -> dev_node is True."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True, "releasable": False})
        assert proj.dev_node is True

    def test_dev_only_with_releasable_string(self):
        """dev_only + releasable = 'tools' -> dev_node is False (part of a releasable)."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True, "releasable": "tools"})
        assert proj.dev_node is False

    def test_not_dev_only_not_dev_node(self):
        """A regular project is not dev_node."""
        proj = WorkspaceProject({"name": "a", "path": "a"})
        assert proj.dev_node is False

    def test_dev_only_implicit_mode(self):
        """dev_only without dev_node or releasable field -> dev_node is False
        (dev_only alone in implicit mode doesn't make it dev_node)."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True})
        # In implicit mode (releasable is None), dev_node depends on legacy flag
        assert proj.dev_node is False


class TestIsReleasableProperty:
    """is_releasable should correctly identify projects that can produce releases."""

    def test_regular_project_is_releasable(self):
        proj = WorkspaceProject({"name": "a", "path": "a"})
        assert proj.is_releasable is True

    def test_releasable_false_not_releasable(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "releasable": False})
        assert proj.is_releasable is False

    def test_legacy_dev_node_not_releasable(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_node": True})
        assert proj.is_releasable is False

    def test_dev_only_with_releasable_string_is_releasable(self):
        """dev_only + releasable = 'tools' -> IS releasable (part of a releasable group)."""
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True, "releasable": "tools"})
        assert proj.is_releasable is True

    def test_dev_only_with_releasable_false_not_releasable(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "dev_only": True, "releasable": False})
        assert proj.is_releasable is False

    def test_explicit_releasable_is_releasable(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "releasable": "core"})
        assert proj.is_releasable is True


# ---------------------------------------------------------------------------
# Release gate tests (releasable membership)
# ---------------------------------------------------------------------------


@pytest.fixture
def split_monorepo(tmp_path, monkeypatch):
    """Create a monorepo with projects testing the split:

    - dev_only_releasable: dev_only=true, releasable='tools' (CAN release)
    - dev_only_non_releasable: dev_only=true, releasable=false (CANNOT release)
    - legacy_dev_node: dev_node=true (backward compat: CANNOT release)
    - regular: no flags (CAN release)
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# split test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    # Write workspace.toml manually to include dev_only + releasable fields
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / "workspace.toml").write_text(
        '[[projects]]\n'
        'path = "dev-rel"\n'
        'name = "dev-rel"\n'
        'dev_only = true\n'
        'releasable = "tools"\n'
        '\n'
        '[[projects]]\n'
        'path = "dev-norel"\n'
        'name = "dev-norel"\n'
        'dev_only = true\n'
        'releasable = false\n'
        '\n'
        '[[projects]]\n'
        'path = "legacy"\n'
        'name = "legacy"\n'
        'dev_node = true\n'
        '\n'
        '[[projects]]\n'
        'path = "regular"\n'
        'name = "regular"\n'
        '\n'
    )

    # Create project directories with minimal structure
    for subdir in ("dev-rel", "dev-norel", "legacy", "regular"):
        proj_dir = tmp_path / subdir
        proj_dir.mkdir()
        (proj_dir / ".rlsbl" / "changes").mkdir(parents=True)
        (proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        (proj_dir / ".rlsbl" / "config.json").write_text(
            json.dumps({"private": False}) + "\n"
        )
        (proj_dir / "package.json").write_text(
            json.dumps({"name": subdir, "version": "0.1.0"})
        )

    run_git(tmp_path, "add", WORKSPACE_DIR)
    for subdir in ("dev-rel", "dev-norel", "legacy", "regular"):
        run_git(tmp_path, "add", subdir)
    run_git(tmp_path, "commit", "-q", "-m", "add projects")

    for subdir in ("dev-rel", "dev-norel", "legacy", "regular"):
        run_git(tmp_path, "tag", f"{subdir}@v0.1.0")

    yield SimpleNamespace(root=tmp_path)


class TestReleaseGateUsesReleasable:
    """Release gates should block on is_releasable, not dev_node."""

    def test_dev_only_releasable_project_can_release(self, split_monorepo):
        """A dev_only=true project with releasable='tools' should pass the release gate."""
        from rlsbl.commands.release.validate import resolve_monorepo_context

        root = split_monorepo.root
        project_dir = root / "dev-rel"

        # resolve_monorepo_context should NOT raise for dev_only + releasable
        name, path, is_lib, is_non_releasable, _rel_name = resolve_monorepo_context(
            str(root), project_dir, lambda msg: None
        )
        assert name == "dev-rel"
        assert is_non_releasable is False  # is_releasable is True, so is_non_releasable is False

    def test_dev_only_non_releasable_project_blocked(self, split_monorepo):
        """A dev_only=true project with releasable=false should be blocked."""
        from rlsbl.commands.release.validate import (
            ReleaseValidationError,
            resolve_monorepo_context,
        )

        root = split_monorepo.root
        project_dir = root / "dev-norel"

        with pytest.raises(ReleaseValidationError, match="non-releasable"):
            resolve_monorepo_context(str(root), project_dir, lambda msg: None)

    def test_legacy_dev_node_still_blocked(self, split_monorepo):
        """A legacy dev_node=true project should still be blocked."""
        from rlsbl.commands.release.validate import (
            ReleaseValidationError,
            resolve_monorepo_context,
        )

        root = split_monorepo.root
        project_dir = root / "legacy"

        with pytest.raises(ReleaseValidationError, match="non-releasable"):
            resolve_monorepo_context(str(root), project_dir, lambda msg: None)

    def test_regular_project_passes(self, split_monorepo):
        """A regular project should pass the release gate."""
        from rlsbl.commands.release.validate import resolve_monorepo_context

        root = split_monorepo.root
        project_dir = root / "regular"

        name, path, is_lib, is_non_releasable, _rel_name = resolve_monorepo_context(
            str(root), project_dir, lambda msg: None
        )
        assert name == "regular"
        assert is_non_releasable is False


# ---------------------------------------------------------------------------
# Boundary check tests (dev_only)
# ---------------------------------------------------------------------------


def _register_and_get_checks():
    """Register all checks on a mock app and return the check function dict."""
    from rlsbl.checks import register_checks

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
    """Create a minimal pyproject.toml."""
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


class TestBoundaryCheckUsesDevOnly:
    """The dev-only-boundary check should use dev_only, not dev_node or is_releasable."""

    def test_dev_only_releasable_still_triggers_boundary(self, tmp_path, monkeypatch):
        """A dev_only=true project triggers boundary check even if releasable='tools'.

        This is the key distinction: dev_only is the boundary flag, releasable
        is the release lifecycle flag. A project can be both dev_only AND
        releasable, and the boundary check should still flag non-dev-only
        projects that depend on it.
        """
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        monkeypatch.chdir(tmp_path)
        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")

        readme = tmp_path / "README.md"
        readme.write_text("# test\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        # Write workspace.toml with dev_only + releasable
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\n'
            'path = "app"\n'
            'name = "app"\n'
            '\n'
            '[[projects]]\n'
            'path = "testlib"\n'
            'name = "testlib"\n'
            'dev_only = true\n'
            '\n'
        )

        # Create pyproject.toml with app depending on testlib
        _make_pypi_project(tmp_path, "app", deps=["testlib"])
        _make_pypi_project(tmp_path, "testlib")

        run_git(tmp_path, "add", WORKSPACE_DIR)
        run_git(tmp_path, "add", "app")
        run_git(tmp_path, "add", "testlib")
        run_git(tmp_path, "commit", "-q", "-m", "add projects")

        projects = load_workspace(str(tmp_path))
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=tmp_path,
            workspace_root=tmp_path,
            config={},
            projects=projects,
            graph=graph,
        )

        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        assert result.status == "fail"
        assert "app" in result.details[0]
        assert "testlib" in result.details[0]

    def test_non_dev_only_releasable_false_no_boundary(self, tmp_path, monkeypatch):
        """A releasable=false project that is NOT dev_only should NOT trigger boundary.

        The boundary check only cares about dev_only, not releasable status.
        """
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        monkeypatch.chdir(tmp_path)
        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")

        readme = tmp_path / "README.md"
        readme.write_text("# test\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        # releasable=false but NOT dev_only
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\n'
            'path = "app"\n'
            'name = "app"\n'
            '\n'
            '[[projects]]\n'
            'path = "util"\n'
            'name = "util"\n'
            'releasable = false\n'
            '\n'
        )

        _make_pypi_project(tmp_path, "app", deps=["util"])
        _make_pypi_project(tmp_path, "util")

        run_git(tmp_path, "add", WORKSPACE_DIR)
        run_git(tmp_path, "add", "app")
        run_git(tmp_path, "add", "util")
        run_git(tmp_path, "commit", "-q", "-m", "add projects")

        projects = load_workspace(str(tmp_path))
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=tmp_path,
            workspace_root=tmp_path,
            config={},
            projects=projects,
            graph=graph,
        )

        checks = _register_and_get_checks()
        result = checks["dev-only-boundary"](ctx)

        # Should pass because util is not dev_only
        assert result.status == "pass"

    def test_check_renamed_from_dev_node_boundary(self):
        """The check should be registered as dev-only-boundary, not dev-node-boundary."""
        checks = _register_and_get_checks()
        assert "dev-only-boundary" in checks
        assert "dev-node-boundary" not in checks


# ---------------------------------------------------------------------------
# Changelog check tests (releasable membership)
# ---------------------------------------------------------------------------


class TestChangelogChecksUseReleasable:
    """Changelog checks should skip based on is_releasable, not dev_node."""

    def _make_ctx_for_project(self, root, project_dir):
        """Create a WorkspaceCheckContext for a specific project."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        return WorkspaceCheckContext(
            project_root=project_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

    def test_non_releasable_skips_coverage_check(self, split_monorepo):
        """Non-releasable project should skip changelog-coverage."""
        root = split_monorepo.root
        ctx = self._make_ctx_for_project(root, root / "dev-norel")
        checks = _register_and_get_checks()
        result = checks["changelog-coverage"](ctx)
        assert result.status == "skip"
        assert "non-releasable" in result.message

    def test_non_releasable_skips_user_facing_check(self, split_monorepo):
        """Non-releasable project should skip changelog-user-facing."""
        root = split_monorepo.root
        ctx = self._make_ctx_for_project(root, root / "dev-norel")
        checks = _register_and_get_checks()
        result = checks["changelog-user-facing"](ctx)
        assert result.status == "skip"
        assert "non-releasable" in result.message

    def test_legacy_dev_node_skips_coverage(self, split_monorepo):
        """Legacy dev_node=true project should skip (is_releasable is False)."""
        root = split_monorepo.root
        ctx = self._make_ctx_for_project(root, root / "legacy")
        checks = _register_and_get_checks()
        result = checks["changelog-coverage"](ctx)
        assert result.status == "skip"
        assert "non-releasable" in result.message

    def test_regular_project_not_skipped(self, split_monorepo):
        """Regular project should NOT be skipped by changelog checks."""
        root = split_monorepo.root
        ctx = self._make_ctx_for_project(root, root / "regular")
        checks = _register_and_get_checks()
        result = checks["changelog-coverage"](ctx)
        # Should not be a "skip" with "non-releasable" message
        assert not (result.status == "skip" and "non-releasable" in result.message)
