"""Tests for monorepo mirror subcommand and subtree-remote-reachable check."""

import json
import os
import subprocess

import pytest

from rlsbl.commands.monorepo import _cmd_mirror
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, save_workspace

from conftest import run_git


def _setup_workspace_with_project(repo, subtree_remote=None):
    """Create a workspace with one npm project, optionally with subtree_remote."""
    proj_dir = repo / "mylib"
    proj_dir.mkdir(exist_ok=True)
    (proj_dir / "package.json").write_text(json.dumps({"name": "mylib", "version": "0.1.0"}))

    proj = {"path": "mylib", "name": "mylib"}
    if subtree_remote:
        proj["subtree_remote"] = subtree_remote

    ws_dir = repo / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    save_workspace(str(repo), [proj])

    run_git(repo, "add", "mylib")
    run_git(repo, "add", WORKSPACE_DIR)
    run_git(repo, "commit", "-q", "-m", "add workspace")


class TestMirrorMissingSubtreeRemote:
    """Mirror command errors when subtree_remote is not configured."""

    def test_missing_subtree_remote_exits(self, mock_git_repo):
        _setup_workspace_with_project(mock_git_repo, subtree_remote=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_missing_subtree_remote_message(self, mock_git_repo, capsys):
        _setup_workspace_with_project(mock_git_repo, subtree_remote=None)
        with pytest.raises(SystemExit):
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        assert "no subtree_remote" in captured.err


class TestMirrorUnreachableRemote:
    """Mirror command errors when subtree_remote is not reachable."""

    def test_unreachable_remote_exits(self, mock_git_repo):
        _setup_workspace_with_project(
            mock_git_repo,
            subtree_remote="https://example.invalid/does-not-exist.git",
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_unreachable_remote_message(self, mock_git_repo, capsys):
        _setup_workspace_with_project(
            mock_git_repo,
            subtree_remote="https://example.invalid/does-not-exist.git",
        )
        with pytest.raises(SystemExit):
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        assert "not reachable" in captured.err


class TestMirrorProjectNotFound:
    """Mirror command errors when project name is not in workspace."""

    def test_unknown_project_exits(self, mock_git_repo):
        _setup_workspace_with_project(mock_git_repo)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "nonexistent"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_unknown_project_message(self, mock_git_repo, capsys):
        _setup_workspace_with_project(mock_git_repo)
        with pytest.raises(SystemExit):
            _cmd_mirror({"project": "nonexistent"}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        assert "not found" in captured.err
        assert "mylib" in captured.err


class TestMirrorNoWorkspace:
    """Mirror command errors when no workspace exists."""

    def test_no_workspace_exits(self, mock_git_repo):
        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "anything"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1


class TestSubtreeRemoteReachableCheck:
    """Tests for the subtree-remote-reachable check."""

    def _make_check_context(self, root):
        """Create a WorkspaceCheckContext for check tests."""
        from pathlib import Path

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), ws_projects)

        return WorkspaceCheckContext(
            project_root=Path(root),
            workspace_root=Path(root),
            config={},
            projects=ws_projects,
            graph=graph,
        )

    def _run_check(self, ctx):
        """Run the subtree-remote-reachable check directly."""
        from rlsbl import app

        check_def = app._check_defs["subtree-remote-reachable"]
        return check_def.impl(ctx)

    def test_skip_when_no_subtree_remotes(self, mock_git_repo):
        """Check skips when no projects have subtree_remote."""
        _setup_workspace_with_project(mock_git_repo, subtree_remote=None)
        ctx = self._make_check_context(mock_git_repo)

        result = self._run_check(ctx)
        assert result.status == "skip"

    def test_fail_when_remote_unreachable(self, mock_git_repo):
        """Check fails when a subtree_remote is unreachable."""
        _setup_workspace_with_project(
            mock_git_repo,
            subtree_remote="https://example.invalid/unreachable.git",
        )
        ctx = self._make_check_context(mock_git_repo)

        result = self._run_check(ctx)
        assert result.status == "fail"
        assert "unreachable" in result.message

    def test_pass_when_remote_reachable(self, mock_git_repo, tmp_path):
        """Check passes when subtree_remote points to a valid local repo."""
        # Create a bare repo to serve as the remote
        bare_repo = tmp_path / "bare-remote.git"
        bare_repo.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q"],
            cwd=str(bare_repo),
            check=True,
        )

        _setup_workspace_with_project(
            mock_git_repo,
            subtree_remote=str(bare_repo),
        )
        ctx = self._make_check_context(mock_git_repo)

        result = self._run_check(ctx)
        assert result.status == "pass"
