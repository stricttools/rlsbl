"""Tests for changelog-exempt project changelog exemption."""

import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import run_git, git_head, make_commit, make_workspace
from rlsbl.commands.pre_push_check import (
    _run_monorepo_check,
    _affected_projects,
)
from rlsbl.context import ProjectContext
from rlsbl.workspace import WORKSPACE_DIR


@pytest.fixture
def exempt_monorepo(tmp_path, monkeypatch):
    """Create a monorepo with one changelog-exempt and one regular project.

    Yields a SimpleNamespace with:
        root        -- Path to the repo root
        projects    -- list of project dicts (matching workspace.toml)
        exempt_dir  -- absolute Path to the changelog-exempt subproject
        regular_dir  -- absolute Path to the regular subproject
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# monorepo test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    projects = [
        {"path": "internal-pkg", "name": "mypkg-internal", "changelog_exempt": True},
        {"path": "regular-pkg", "name": "mypkg-regular"},
    ]

    make_workspace(tmp_path, projects)

    exempt_dir = tmp_path / "internal-pkg"
    regular_dir = tmp_path / "regular-pkg"

    # Set up changelog-exempt project with JSONL changelog
    (exempt_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (exempt_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (exempt_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    (exempt_dir / "package.json").write_text(
        json.dumps({"name": "mypkg-internal", "version": "0.1.0"})
    )

    # Set up regular project with JSONL changelog
    (regular_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (regular_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (regular_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    (regular_dir / "package.json").write_text(
        json.dumps({"name": "mypkg-regular", "version": "0.1.0"})
    )

    # Commit all
    run_git(tmp_path, "add", WORKSPACE_DIR)
    run_git(tmp_path, "add", "internal-pkg")
    run_git(tmp_path, "add", "regular-pkg")
    run_git(tmp_path, "commit", "-q", "-m", "add monorepo projects")

    # Tag both projects
    run_git(tmp_path, "tag", "mypkg-internal@v0.1.0")
    run_git(tmp_path, "tag", "mypkg-regular@v0.1.0")

    yield SimpleNamespace(
        root=tmp_path,
        projects=projects,
        exempt_dir=exempt_dir,
        regular_dir=regular_dir,
    )


def _write_jsonl_entry(proj_dir, commits, user_facing=False):
    """Append a JSONL entry to a project's unreleased.jsonl."""
    changes_dir = proj_dir / ".rlsbl" / "changes"
    entry = {"commits": commits, "user_facing": user_facing}
    if user_facing:
        entry["description"] = "Test change."
        entry["type"] = "feature"
    with open(changes_dir / "unreleased.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


class TestChangelogExemptProjectPrePush:
    """Changelog-exempt projects should be skipped in pre-push changelog checks."""

    def test_exempt_project_skips_pre_push_coverage(self, exempt_monorepo, capsys):
        """Push touching changelog-exempt project without JSONL entries exits 0."""
        root = exempt_monorepo.root
        projects = exempt_monorepo.projects

        # Make a commit that touches the changelog-exempt project
        sha = make_commit(root, "internal-pkg/main.js", "exempt change")

        # Do NOT add any JSONL entry for the changelog-exempt project
        changed_files = {"internal-pkg/main.js"}

        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            # Should pass because changelog-exempt project is skipped
            assert exc_info.value.code == 0

    def test_non_exempt_still_enforced(self, exempt_monorepo, capsys):
        """Regular (non-exempt) project still requires JSONL coverage."""
        root = exempt_monorepo.root
        projects = exempt_monorepo.projects

        # Make a commit that touches the regular project
        sha = make_commit(root, "regular-pkg/main.js", "regular change")

        # Do NOT add any JSONL entry for the regular project
        changed_files = {"regular-pkg/main.js"}

        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            # Should fail because regular project lacks coverage
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "mypkg-regular" in captured.err

    def test_exempt_skipped_while_regular_checked(self, exempt_monorepo, capsys):
        """When both projects have changes, exempt is skipped and regular is checked."""
        root = exempt_monorepo.root
        projects = exempt_monorepo.projects

        # Make a commit touching both
        (root / "internal-pkg" / "a.js").write_text("// a\n")
        (root / "regular-pkg" / "b.js").write_text("// b\n")
        run_git(root, "add", "internal-pkg/a.js", "regular-pkg/b.js")
        run_git(root, "commit", "-q", "-m", "cross-project change")
        sha = git_head(root)

        # Add coverage ONLY for regular project
        _write_jsonl_entry(exempt_monorepo.regular_dir, [sha[:12]])
        run_git(root, "add", "regular-pkg/.rlsbl/changes/unreleased.jsonl")
        run_git(root, "commit", "-q", "-m", "changelog: regular entry")

        changed_files = {"internal-pkg/a.js", "regular-pkg/b.js"}

        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            # Should pass: exempt is skipped, regular has coverage
            assert exc_info.value.code == 0


class TestChangelogExemptProjectChecks:
    """Changelog-exempt projects should skip changelog-coverage and changelog-user-facing checks."""

    def test_exempt_project_skips_user_facing_check(self, exempt_monorepo):
        """Changelog-user-facing check returns skip for changelog-exempt projects."""
        from strictcli import CheckResult
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace, resolve_project
        from rlsbl.workspace_graph import WorkspaceGraph

        root = exempt_monorepo.root
        exempt_dir = exempt_monorepo.exempt_dir

        # Load workspace state
        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        # Create a WorkspaceCheckContext pointing at the changelog-exempt project
        ctx = WorkspaceCheckContext(
            project_root=exempt_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        # Import and call the check function
        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        # Collect registered checks
        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        # Run changelog-user-facing check
        result = registered_checks["changelog-user-facing"](ctx)
        assert result.status == "skip"
        assert "changelog-exempt" in result.message

    def test_exempt_project_skips_coverage_check(self, exempt_monorepo):
        """Changelog-coverage check returns skip for changelog-exempt projects."""
        from strictcli import CheckResult
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        root = exempt_monorepo.root
        exempt_dir = exempt_monorepo.exempt_dir

        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        ctx = WorkspaceCheckContext(
            project_root=exempt_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        result = registered_checks["changelog-coverage"](ctx)
        assert result.status == "skip"
        assert "changelog-exempt" in result.message

    def test_regular_project_not_skipped(self, exempt_monorepo):
        """Changelog checks are NOT skipped for regular (non-exempt) projects."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        root = exempt_monorepo.root
        regular_dir = exempt_monorepo.regular_dir

        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        ctx = WorkspaceCheckContext(
            project_root=regular_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        # Coverage check should NOT skip for regular project
        result = registered_checks["changelog-coverage"](ctx)
        assert result.status != "skip" or "changelog-exempt" not in result.message

        # User-facing check should NOT skip for regular project
        result = registered_checks["changelog-user-facing"](ctx)
        assert result.status != "skip" or "changelog-exempt" not in result.message
