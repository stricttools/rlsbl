"""Tests for the workspace-unbuildable check."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.context import ProjectContext


class TestWorkspaceUnbuildableSkips:
    """The check skips when the context is not a workspace or has no pypi targets."""

    def test_skips_non_workspace(self, mock_git_repo):
        """Non-workspace context -> skip (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert result.status == "skip"
        assert "not a monorepo" in result.message

    def test_skips_no_pypi_targets(self, mock_git_repo):
        """Workspace with no pypi-target projects -> skip."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text('{"name":"mylib","version":"1.0.0"}')

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )
        result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "skip"
        assert "no pypi-target" in result.message

    def test_skips_uv_not_installed(self, mock_git_repo):
        """uv not installed -> skip."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )
        with patch("subprocess.run", side_effect=FileNotFoundError("uv")):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "skip"
        assert "uv not installed" in result.message


class TestWorkspaceUnbuildablePass:
    """The check passes when uv sync --all-packages --dry-run succeeds."""

    def test_passes_when_sync_succeeds(self, mock_git_repo):
        """uv sync dry-run exits 0 -> pass."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        fake_result = subprocess.CompletedProcess(
            args=["uv", "sync", "--all-packages", "--dry-run"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "pass"
        assert "all workspace members buildable" in result.message


class TestWorkspaceUnbuildableFail:
    """The check fails when uv sync --all-packages --dry-run fails."""

    def test_fails_when_sync_fails(self, mock_git_repo):
        """uv sync dry-run exits non-zero -> fail with details."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        fake_result = subprocess.CompletedProcess(
            args=["uv", "sync", "--all-packages", "--dry-run"],
            returncode=1,
            stdout="",
            stderr="error: Failed to build `broken-pkg`\nCaused by: missing build-system in pyproject.toml",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "fail"
        assert "broken-pkg" in result.message
        assert len(result.details) == 2

    def test_fails_on_timeout(self, mock_git_repo):
        """uv sync dry-run times out -> fail."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("uv", 120)):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "fail"
        assert "timed out" in result.message
