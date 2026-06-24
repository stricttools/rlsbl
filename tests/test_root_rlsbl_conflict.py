"""Tests for Phase 4: preventing coexistence of root .rlsbl/ and .rlsbl-monorepo/."""

import os

import pytest

from strictcli import CheckResult

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.commands.init_cmd import _is_workspace_root, run_cmd, run_cmd_multi


# ---------------------------------------------------------------------------
# 4a: root-rlsbl-conflict check
# ---------------------------------------------------------------------------


class TestRootRlsblConflictCheck:
    """The root-rlsbl-conflict check detects when both .rlsbl/ and .rlsbl-monorepo/ exist."""

    def test_fail_both_exist(self, mock_git_repo):
        """Both .rlsbl/ and .rlsbl-monorepo/ at workspace root -> fail."""
        (mock_git_repo / ".rlsbl").mkdir()
        monorepo_dir = mock_git_repo / ".rlsbl-monorepo"
        monorepo_dir.mkdir()
        (monorepo_dir / "workspace.toml").write_text("[[packages]]\n")

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["root-rlsbl-conflict"].impl(ctx)
        assert result.status == "fail"
        assert ".rlsbl/" in result.message
        assert ".rlsbl-monorepo/" in result.message

    def test_pass_only_monorepo(self, mock_git_repo):
        """Only .rlsbl-monorepo/ exists -> pass."""
        monorepo_dir = mock_git_repo / ".rlsbl-monorepo"
        monorepo_dir.mkdir()
        (monorepo_dir / "workspace.toml").write_text("[[packages]]\n")

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["root-rlsbl-conflict"].impl(ctx)
        assert result.status == "pass"

    def test_pass_only_rlsbl(self, mock_git_repo):
        """Only .rlsbl/ exists (standalone project) -> pass."""
        (mock_git_repo / ".rlsbl").mkdir()

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["root-rlsbl-conflict"].impl(ctx)
        assert result.status == "pass"

    def test_pass_neither_exists(self, mock_git_repo):
        """Neither .rlsbl/ nor .rlsbl-monorepo/ exists -> pass."""
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["root-rlsbl-conflict"].impl(ctx)
        assert result.status == "pass"

    def test_skip_non_workspace_context(self, mock_git_repo):
        """Non-workspace context -> skip (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        cdef = app._check_defs["root-rlsbl-conflict"]
        result = scope_adapter(ctx, cdef.scope)
        assert result.status == "skip"
        assert "not a monorepo" in result.message


# ---------------------------------------------------------------------------
# 4b: scaffold guard at workspace root
# ---------------------------------------------------------------------------


class TestScaffoldWorkspaceRootGuard:
    """Scaffold at a workspace root skips entirely (returns early, creates nothing)."""

    def test_run_cmd_skips_at_workspace_root(self, mock_git_repo, capsys):
        """run_cmd returns early at a workspace root without creating .rlsbl/."""
        # Set up workspace root marker
        monorepo_dir = mock_git_repo / ".rlsbl-monorepo"
        monorepo_dir.mkdir()
        (monorepo_dir / "workspace.toml").write_text("[[packages]]\n")

        # Create a package.json so the target detection would find something
        (mock_git_repo / "package.json").write_text('{"name":"test","version":"1.0.0"}')

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})

        run_cmd("npm", {}, {}, ctx)

        captured = capsys.readouterr()
        assert "Skipping scaffold at workspace root" in captured.out
        # .rlsbl/ should NOT be created
        assert not (mock_git_repo / ".rlsbl").exists()

    def test_run_cmd_multi_skips_at_workspace_root(self, mock_git_repo, capsys):
        """run_cmd_multi returns early at a workspace root without creating .rlsbl/."""
        # Set up workspace root marker
        monorepo_dir = mock_git_repo / ".rlsbl-monorepo"
        monorepo_dir.mkdir()
        (monorepo_dir / "workspace.toml").write_text("[[packages]]\n")

        # Create project files
        (mock_git_repo / "package.json").write_text('{"name":"test","version":"1.0.0"}')
        (mock_git_repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        from rlsbl.context import ProjectContext
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})

        run_cmd_multi(["npm", "pypi"], {}, {}, ctx)

        captured = capsys.readouterr()
        assert "Skipping scaffold at workspace root" in captured.out
        # .rlsbl/ should NOT be created
        assert not (mock_git_repo / ".rlsbl").exists()
