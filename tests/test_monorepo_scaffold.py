"""Tests for scaffold + monorepo integration.

Covers:
- scaffold triggering sync when inside a monorepo
- scaffold standalone (no monorepo) not triggering sync
- monorepo add auto-scaffolding unscaffolded projects
- monorepo add skipping scaffold for already-scaffolded projects
- monorepo add committing workspace.toml
- monorepo init committing workspace.toml
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import _cmd_init, _cmd_add
from rlsbl.commands.init_cmd import _trigger_monorepo_sync
from rlsbl.workspace import load_workspace, WORKSPACE_DIR, WORKSPACE_FILE


CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo test
"""


def _make_npm_project(base_path, subdir):
    """Create a minimal npm project (package.json) so detect_targets finds it."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + subdir, "version": "0.1.0"}, f)
    return subdir


def _make_npm_project_with_ci(base_path, subdir):
    """Create an npm project with CI workflow so sync has something to copy."""
    _make_npm_project(base_path, subdir)
    wf_dir = os.path.join(str(base_path), subdir, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
        f.write(CI_WORKFLOW)
    return subdir


def _scaffold_project(base_path, subdir):
    """Create .rlsbl/config.json to mark a project as already scaffolded."""
    rlsbl_dir = os.path.join(str(base_path), subdir, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
        json.dump({"targets": ["npm"]}, f)


class TestScaffoldTriggersSync:
    def test_trigger_calls_sync_in_monorepo(self, mock_git_repo):
        """_trigger_monorepo_sync runs sync subprocess when inside a monorepo."""
        _cmd_init({}, project_root=".")
        # _cmd_init auto-commits workspace.toml, so workspace is ready

        calls = []

        def tracking_run(cmd, *args, **kwargs):
            calls.append(cmd)
            # Don't actually run the sync subprocess
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=tracking_run):
            _trigger_monorepo_sync()

        sync_calls = [
            c for c in calls
            if isinstance(c, list) and "monorepo" in c and "sync" in c
        ]
        assert len(sync_calls) == 1

    def test_trigger_noop_without_monorepo(self, mock_git_repo):
        """_trigger_monorepo_sync does nothing when not inside a monorepo."""
        calls = []
        original_run = subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=tracking_run):
            _trigger_monorepo_sync()

        # No subprocess calls should have been made
        assert len(calls) == 0

    def test_trigger_ignores_sync_failure(self, mock_git_repo):
        """_trigger_monorepo_sync silently ignores sync failures."""
        _cmd_init({}, project_root=".")
        # _cmd_init auto-commits workspace.toml, so workspace is ready

        def failing_run(cmd, *args, **kwargs):
            raise OSError("subprocess failed")

        # Should not raise
        with patch("subprocess.run", side_effect=failing_run):
            _trigger_monorepo_sync()


class TestAddAutoScaffolds:
    def test_add_auto_scaffolds_unscaffolded_project(self, mock_git_repo):
        """monorepo add runs scaffold when project has no .rlsbl/config.json."""
        _cmd_init({}, project_root=".")
        _make_npm_project_with_ci(mock_git_repo, "pkg-a")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        scaffold_calls = []
        sync_calls = []
        original_run = subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                if "scaffold" in cmd:
                    scaffold_calls.append(cmd)
                    return subprocess.CompletedProcess(args=cmd, returncode=0)
                elif "monorepo" in cmd and "sync" in cmd:
                    sync_calls.append(cmd)
                    return subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=tracking_run):
            _cmd_add(["pkg-a"], {}, project_root=".")

        assert len(scaffold_calls) == 1
        assert len(sync_calls) == 1

    def test_add_skips_scaffold_if_already_done(self, mock_git_repo, capsys):
        """monorepo add does NOT re-scaffold when .rlsbl/config.json exists."""
        _cmd_init({}, project_root=".")
        _make_npm_project_with_ci(mock_git_repo, "pkg-a")
        _scaffold_project(mock_git_repo, "pkg-a")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        scaffold_calls = []
        original_run = subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list):
                if "scaffold" in cmd:
                    scaffold_calls.append(cmd)
                    return subprocess.CompletedProcess(args=cmd, returncode=0)
                elif "monorepo" in cmd and "sync" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=tracking_run):
            _cmd_add(["pkg-a"], {}, project_root=".")

        # Scaffold should NOT have been called
        assert len(scaffold_calls) == 0
        captured = capsys.readouterr()
        assert "Scaffolding" not in captured.out


class TestAddCommitsWorkspace:
    def test_add_commits_workspace_toml(self, mock_git_repo):
        """monorepo add commits workspace.toml after adding a project."""
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_init({}, project_root=".")
        _make_npm_project_with_ci(mock_git_repo, "pkg-a")
        _scaffold_project(mock_git_repo, "pkg-a")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        # Mock sync subprocess and use plain git for auto-commit
        original_run = subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "monorepo" in cmd and "sync" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("rlsbl.utils.find_commit_tool", return_value="git"), \
             patch("subprocess.run", side_effect=tracking_run):
            _cmd_add(["pkg-a"], {}, project_root=".")

        # Check that workspace.toml was committed
        result = subprocess.run(
            ["git", "log", "--oneline", "--all"],
            cwd=str(mock_git_repo),
            capture_output=True, text=True,
        )
        assert "monorepo: add pkg-a" in result.stdout


class TestInitCommitsWorkspace:
    def test_init_commits_workspace_toml(self, mock_git_repo):
        """monorepo init commits workspace.toml."""
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_init({}, project_root=".")

        result = subprocess.run(
            ["git", "log", "--oneline", "--all"],
            cwd=str(mock_git_repo),
            capture_output=True, text=True,
        )
        assert "monorepo: init workspace" in result.stdout

    def test_init_workspace_is_committed(self, mock_git_repo):
        """After init, workspace.toml should not show up as untracked or modified."""
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_init({}, project_root=".")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(mock_git_repo),
            capture_output=True, text=True,
        )
        ws_file = os.path.join(WORKSPACE_DIR, WORKSPACE_FILE)
        # workspace.toml should not appear in git status (it's been committed)
        assert ws_file not in result.stdout
