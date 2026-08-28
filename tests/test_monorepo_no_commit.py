"""Tests for the --no-auto-commit flag on monorepo init, add, and sync."""

import json
import os
import subprocess

import pytest

from rlsbl.commands.monorepo import _cmd_init, _cmd_add, _cmd_sync


CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""


def _git_log_count(repo):
    """Count commits reachable from HEAD in the given repo."""
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _make_npm_project(base_path, subdir, with_ci=False):
    """Create a minimal npm project so detect_targets finds it."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + os.path.basename(subdir), "version": "0.1.0"}, f)

    if with_ci:
        wf_dir = os.path.join(proj_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
            f.write(CI_WORKFLOW)
    return subdir


class TestInitNoCommit:
    def test_init_no_commit_does_not_create_commit(self, mock_git_repo, capsys):
        """`monorepo init --no-commit` must not create a git commit."""
        before = _git_log_count(mock_git_repo)
        _cmd_init({"auto-commit": False, "root-dev-node": True}, project_root=".")
        after = _git_log_count(mock_git_repo)
        assert after == before, "init --no-commit should not create a commit"

    def test_init_default_creates_commit(self, mock_git_repo, capsys):
        """`monorepo init` (no flag) must still auto-commit (backward compat)."""
        before = _git_log_count(mock_git_repo)
        _cmd_init({"root-dev-node": True}, project_root=".")
        after = _git_log_count(mock_git_repo)
        assert after == before + 1, "default init should produce exactly one commit"

    def test_init_no_commit_prints_message(self, mock_git_repo, capsys):
        """`monorepo init --no-commit` must print a clear skip message."""
        _cmd_init({"auto-commit": False, "root-dev-node": True}, project_root=".")
        captured = capsys.readouterr()
        assert "--no-auto-commit" in captured.out
        assert "safegit commit" in captured.out


class TestAddNoCommit:
    def test_add_no_commit_does_not_create_commit(self, mock_git_repo, capsys):
        """`monorepo add --no-commit` must not create any commit, including
        from the auto-triggered scaffold and sync sub-commands."""
        _cmd_init({"root-dev-node": True}, project_root=".")  # default init creates a commit; record after that
        before = _git_log_count(mock_git_repo)
        _make_npm_project(mock_git_repo, "pkg-a", with_ci=True)
        _cmd_add(["pkg-a"], {"releasable": "false", "auto-commit": False}, project_root=".")
        after = _git_log_count(mock_git_repo)
        assert after == before, (
            f"add --no-commit should produce zero commits "
            f"(including from auto-scaffold/sync), got {after - before}"
        )

    def test_add_default_creates_commit(self, mock_git_repo, capsys):
        """`monorepo add` (no flag) must still auto-commit workspace.toml."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        before = _git_log_count(mock_git_repo)
        _make_npm_project(mock_git_repo, "pkg-a", with_ci=True)
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        after = _git_log_count(mock_git_repo)
        # We expect at least one commit (workspace.toml add). The auto-triggered
        # scaffold and sync may also commit. Either way, count must increase.
        assert after > before, "default add should produce at least one commit"

    def test_add_no_commit_prints_message(self, mock_git_repo, capsys):
        """`monorepo add --no-commit` must print the skip message."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a", with_ci=True)
        capsys.readouterr()  # drain prior output
        _cmd_add(["pkg-a"], {"releasable": "false", "auto-commit": False}, project_root=".")
        captured = capsys.readouterr()
        assert "--no-auto-commit" in captured.out
        assert "safegit commit" in captured.out


class TestSyncNoCommit:
    def _setup_workspace(self, mock_git_repo):
        """Create init + one project + one CI workflow, all committed."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a", with_ci=True)
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        # Commit any remaining untracked files from auto-scaffold so the
        # working tree is clean before testing sync.
        subprocess.run(["git", "add", "-A"], cwd=str(mock_git_repo), check=True)
        # commit -A can be a no-op if everything already committed.
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(mock_git_repo),
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-q", "-m", "setup"],
                cwd=str(mock_git_repo), check=True,
            )

    def test_sync_no_commit_does_not_create_commit(self, mock_git_repo, capsys):
        """`monorepo sync --no-commit` must not create a commit."""
        self._setup_workspace(mock_git_repo)
        # Remove any synced workflows so sync actually writes new files.
        root_wf = mock_git_repo / ".github" / "workflows"
        if root_wf.exists():
            for f in list(root_wf.iterdir()):
                f.chmod(0o644)
                f.unlink()
            subprocess.run(["git", "add", "-A"], cwd=str(mock_git_repo), check=True)
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(mock_git_repo),
            )
            if result.returncode != 0:
                subprocess.run(
                    ["git", "commit", "-q", "-m", "remove synced"],
                    cwd=str(mock_git_repo), check=True,
                )
        before = _git_log_count(mock_git_repo)
        _cmd_sync({"auto-commit": False}, project_root=".")
        after = _git_log_count(mock_git_repo)
        assert after == before, "sync --no-commit should not create a commit"

    def test_sync_no_commit_prints_message(self, mock_git_repo, capsys):
        """`monorepo sync --no-commit` must print the skip message."""
        self._setup_workspace(mock_git_repo)
        capsys.readouterr()  # drain prior output
        _cmd_sync({"auto-commit": False}, project_root=".")
        captured = capsys.readouterr()
        assert "--no-auto-commit" in captured.out
        assert "safegit commit" in captured.out

    def test_sync_default_creates_commit_when_files_written(self, mock_git_repo, capsys):
        """`monorepo sync` (no flag) must auto-commit synced workflow files."""
        self._setup_workspace(mock_git_repo)
        # Remove synced workflows so sync produces fresh files to commit.
        root_wf = mock_git_repo / ".github" / "workflows"
        if root_wf.exists():
            for f in list(root_wf.iterdir()):
                f.chmod(0o644)
                f.unlink()
            subprocess.run(["git", "add", "-A"], cwd=str(mock_git_repo), check=True)
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(mock_git_repo),
            )
            if result.returncode != 0:
                subprocess.run(
                    ["git", "commit", "-q", "-m", "remove synced"],
                    cwd=str(mock_git_repo), check=True,
                )
        before = _git_log_count(mock_git_repo)
        _cmd_sync({}, project_root=".")
        after = _git_log_count(mock_git_repo)
        assert after == before + 1, (
            f"default sync should produce one commit when files are written, "
            f"got {after - before}"
        )


class TestSyncCommitsIntoTheWorkspaceItWasHanded:
    """sync's auto-commit belongs to the workspace argument, not the cwd.

    ``_cmd_sync`` takes the project root it should sync as an argument and
    resolves the workspace root from it, so the commit it makes must go to
    THAT repository. It used to run the commit from the process cwd instead,
    which made auto-commit unusable for any caller pointing sync at a
    workspace other than the one it happens to be standing in -- and, when the
    cwd was itself a git repo, made git refuse paths outside it.
    """

    def _build_workspace(self, root):
        """init + one project with a CI workflow, committed, inside *root*."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.local"], cwd=str(root), check=True,
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
        (root / "README.md").write_text("# other\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(root), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(root), check=True)

        cwd = os.getcwd()
        os.chdir(str(root))
        try:
            _cmd_init({"root-dev-node": True}, project_root=".")
            _make_npm_project(root, "pkg-a", with_ci=True)
            _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        finally:
            os.chdir(cwd)

        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        if subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=str(root),
        ).returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=str(root), check=True)

        # Remove the synced workflows so the next sync has files to write.
        wf = root / ".github" / "workflows"
        if wf.exists():
            for f in list(wf.iterdir()):
                f.chmod(0o644)
                f.unlink()
            subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
            if subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=str(root),
            ).returncode != 0:
                subprocess.run(
                    ["git", "commit", "-q", "-m", "remove synced"],
                    cwd=str(root), check=True,
                )

    def test_commit_goes_to_the_workspace_not_the_cwd(self, mock_git_repo):
        other = mock_git_repo.parent / "other-workspace"
        other.mkdir()
        self._build_workspace(other)

        before_other = _git_log_count(other)
        before_cwd = _git_log_count(mock_git_repo)

        _cmd_sync({}, project_root=str(other))

        assert _git_log_count(other) > before_other, (
            "sync must commit into the workspace it was handed"
        )
        assert _git_log_count(mock_git_repo) == before_cwd, (
            "sync must not commit into the process cwd's repository"
        )
        assert subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(other),
            capture_output=True, text=True, check=True,
        ).stdout.strip() == "", "sync left the workspace dirty"
