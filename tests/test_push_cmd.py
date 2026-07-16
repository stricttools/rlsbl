"""Tests for rlsbl push: tool-mediated push with branch guard,
coverage preflight with remediation, behind-remote refusal, and
RLSBL_RELEASE_PUSH absence verification.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import git_head, make_commit, make_ctx, run_git

from rlsbl.commands.push_cmd import (
    _build_coverage_refs,
    _check_behind_remote,
    _check_branch_guard,
    _check_orphaned_entries,
    _format_uncovered_hint,
    run_push,
)
from rlsbl.context import ProjectContext


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def push_repo(tmp_path, monkeypatch):
    """Create a git repo with .rlsbl/changes/ and a remote, ready for push testing.

    Returns the repo Path. The repo has:
    - An initial commit and v0.0.0 tag
    - An empty unreleased.jsonl
    - A local bare remote (for behind-remote testing)
    - config.json with release_branches=["main"]
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    # Create a bare remote
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(bare)],
        check=True, capture_output=True,
    )

    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "remote", "add", "origin", str(bare))

    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")
    run_git(repo, "tag", "v0.0.0")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({
            "publish_mode": "ci",
            "targets": [],
            "release_branches": ["main"],
        })
    )

    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

    # Push initial state to remote so origin/main exists
    run_git(repo, "push", "-u", "origin", "main")

    return repo


def _make_push_ctx(repo, config=None):
    """Create a ProjectContext for push testing."""
    if config is None:
        config_path = repo / ".rlsbl" / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
        else:
            config = {}
    return ProjectContext(
        project_root=Path(repo),
        workspace_root=None,
        config=config,
    )


# ------------------------------------------------------------------
# Test 1: Push to a release branch -> error naming the branch
# ------------------------------------------------------------------


class TestBranchGuard:
    """Push to a release branch is refused with an error naming the branch."""

    def test_release_branch_error(self):
        error = _check_branch_guard("main", ["main", "master"])
        assert error is not None
        assert "main" in error
        assert "rlsbl release run" in error

    def test_non_release_branch_allowed(self):
        error = _check_branch_guard("feat/my-feature", ["main", "master"])
        assert error is None

    def test_push_to_release_branch_exits(self, push_repo, monkeypatch):
        """Integration: run_push on main (a release branch) exits with error."""
        ctx = _make_push_ctx(push_repo)
        with pytest.raises(SystemExit) as exc_info:
            run_push(ctx, yes=True, quiet=True)
        assert exc_info.value.code == 1


# ------------------------------------------------------------------
# Test 2: Push with uncovered commits -> error with remediation hint
# ------------------------------------------------------------------


class TestUncoveredCommits:
    """Uncovered commits produce an error with remediation hints."""

    def test_format_uncovered_hint_includes_shas(self):
        msg = "JSONL changelog missing coverage for 2 commit(s): abc123def456, fed654321cba"
        hint = _format_uncovered_hint(msg)
        assert "abc123def456" in hint
        assert "fed654321cba" in hint
        assert "rlsbl changelog add --commits" in hint

    def test_uncovered_commit_blocks_push(self, push_repo, monkeypatch):
        """A commit without a JSONL entry blocks the push."""
        # Switch to a dev branch
        run_git(push_repo, "checkout", "-b", "dev")

        # Make an uncovered commit
        (push_repo / "src.py").write_text("x = 1\n")
        run_git(push_repo, "add", "src.py")
        run_git(push_repo, "commit", "-q", "-m", "feat: new feature")

        ctx = _make_push_ctx(push_repo)
        with pytest.raises(SystemExit) as exc_info:
            run_push(ctx, yes=True, quiet=True)
        assert exc_info.value.code == 1


# ------------------------------------------------------------------
# Test 3: Push with stale entries -> error mentioning changelog remap
# ------------------------------------------------------------------


class TestStaleEntries:
    """Stale/orphaned entries produce an error mentioning changelog remap."""

    def test_orphaned_entries_detected(self, push_repo, monkeypatch):
        """Entries with fully unresolvable commits are detected."""
        # Switch to a dev branch
        run_git(push_repo, "checkout", "-b", "dev")

        # Write a JSONL entry with a fake (unresolvable) commit hash
        changes = push_repo / ".rlsbl" / "changes"
        fake_sha = "a" * 40
        entry = json.dumps({
            "commits": [fake_sha],
            "user_facing": True,
            "description": "stale entry",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")

        orphan_errors = _check_orphaned_entries(str(push_repo))
        assert len(orphan_errors) > 0
        assert "rlsbl changelog remap" in orphan_errors[0]

    def test_stale_entries_block_push(self, push_repo, monkeypatch):
        """Stale entries block the push with a remap hint."""
        # Switch to a dev branch
        run_git(push_repo, "checkout", "-b", "dev")

        # Make a real commit so the branch has something to push
        (push_repo / "src.py").write_text("x = 1\n")
        run_git(push_repo, "add", "src.py")
        run_git(push_repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(push_repo)

        # Write a JSONL entry with a stale (unresolvable) commit
        # AND a valid entry for the real commit (so coverage alone would pass)
        changes = push_repo / ".rlsbl" / "changes"
        fake_sha = "b" * 40
        entries = [
            json.dumps({
                "commits": [fake_sha],
                "user_facing": True,
                "description": "stale entry",
                "type": "feature",
            }),
            json.dumps({
                "commits": [head_sha],
                "user_facing": True,
                "description": "valid entry",
                "type": "feature",
            }),
        ]
        (changes / "unreleased.jsonl").write_text("\n".join(entries) + "\n")

        ctx = _make_push_ctx(push_repo)
        with pytest.raises(SystemExit) as exc_info:
            run_push(ctx, yes=True, quiet=True)
        assert exc_info.value.code == 1


# ------------------------------------------------------------------
# Test 4: Push when behind remote -> error with commit count
# ------------------------------------------------------------------


class TestBehindRemote:
    """Push when local branch is behind remote produces a clear error."""

    def test_behind_remote_error(self, push_repo, monkeypatch):
        """When remote has commits the local branch doesn't, push is refused."""
        # Switch to a dev branch, push it
        run_git(push_repo, "checkout", "-b", "dev")
        (push_repo / "dev1.txt").write_text("dev work\n")
        run_git(push_repo, "add", "dev1.txt")
        run_git(push_repo, "commit", "-q", "-m", "dev commit 1")
        head_sha = git_head(push_repo)

        # Cover the commit in changelog
        changes = push_repo / ".rlsbl" / "changes"
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": False,
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(push_repo, "add", ".rlsbl")
        run_git(push_repo, "commit", "-q", "-m", "add changelog entry")
        head_sha2 = git_head(push_repo)
        # Also cover the changelog commit
        entry2 = json.dumps({
            "commits": [head_sha2],
            "user_facing": False,
        })
        (changes / "unreleased.jsonl").write_text(
            entry + "\n" + entry2 + "\n"
        )
        run_git(push_repo, "add", ".rlsbl")
        run_git(push_repo, "commit", "-q", "--amend", "-m", "add changelog entry")

        run_git(push_repo, "push", "-u", "origin", "dev")

        # Simulate: remote gets a commit that local doesn't have
        # Clone the bare repo to a temp dir, push a commit, then go back
        tmp_clone = push_repo.parent / "clone"
        bare = push_repo.parent / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(tmp_clone)],
            check=True, capture_output=True,
        )
        run_git(tmp_clone, "checkout", "dev")
        run_git(tmp_clone, "config", "user.email", "other@test.local")
        run_git(tmp_clone, "config", "user.name", "Other")
        (tmp_clone / "remote_file.txt").write_text("remote work\n")
        run_git(tmp_clone, "add", "remote_file.txt")
        run_git(tmp_clone, "commit", "-q", "-m", "remote commit")
        run_git(tmp_clone, "push", "origin", "dev")

        # Back in our repo: fetch to see the remote ahead
        error = _check_behind_remote("dev")
        assert error is not None
        assert "behind" in error
        assert "origin/dev" in error
        # Should mention the count
        assert "1 commit(s)" in error

    def test_not_behind_no_error(self, push_repo, monkeypatch):
        """When local is up to date, no behind-remote error."""
        run_git(push_repo, "checkout", "-b", "dev2")
        (push_repo / "dev2.txt").write_text("dev work\n")
        run_git(push_repo, "add", "dev2.txt")
        run_git(push_repo, "commit", "-q", "-m", "dev commit")
        run_git(push_repo, "push", "-u", "origin", "dev2")

        error = _check_behind_remote("dev2")
        assert error is None


# ------------------------------------------------------------------
# Test 5: Successful push to a non-release branch (mocked git push)
# ------------------------------------------------------------------


class TestSuccessfulPush:
    """Successful push on a non-release branch with all checks passing."""

    def test_successful_push(self, push_repo, monkeypatch):
        """Push succeeds when branch is not release, commits are covered,
        and branch is not behind remote."""
        # Switch to dev branch
        run_git(push_repo, "checkout", "-b", "dev-success")

        # Make a commit and cover it
        (push_repo / "feature.py").write_text("def hello(): pass\n")
        run_git(push_repo, "add", "feature.py")
        run_git(push_repo, "commit", "-q", "-m", "add feature")
        head_sha = git_head(push_repo)

        changes = push_repo / ".rlsbl" / "changes"
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "Add hello feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(push_repo, "add", ".rlsbl")
        run_git(push_repo, "commit", "-q", "-m", "changelog")

        # Cover the changelog commit too (it may be auto-exempted, but be safe)
        chlog_sha = git_head(push_repo)

        ctx = _make_push_ctx(push_repo)

        # Mock subprocess.run for the actual git push to avoid real network
        push_calls = []
        original_run = subprocess.run

        def mock_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "git" and "push" in cmd:
                # Check that RLSBL_RELEASE_PUSH is NOT in the env
                env = kwargs.get("env") or os.environ
                assert "RLSBL_RELEASE_PUSH" not in env or env.get("RLSBL_RELEASE_PUSH") != "1"
                push_calls.append(cmd)
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        with patch("rlsbl.commands.push_cmd.subprocess.run", side_effect=mock_run):
            run_push(ctx, yes=True, quiet=False)

        # Verify git push was called
        assert len(push_calls) == 1
        assert "dev-success" in push_calls[0]


# ------------------------------------------------------------------
# Test 6: Pre-push hook also runs (RLSBL_RELEASE_PUSH is NOT set)
# ------------------------------------------------------------------


class TestNoReleasePushEnv:
    """Verify that RLSBL_RELEASE_PUSH is NOT set during rlsbl push."""

    def test_no_release_push_env_in_push(self, push_repo, monkeypatch):
        """The push subprocess must NOT have RLSBL_RELEASE_PUSH=1 in its env.

        This ensures the pre-push hook runs its full checks as a backstop.
        """
        # Switch to dev branch
        run_git(push_repo, "checkout", "-b", "dev-env-test")

        (push_repo / "env_test.py").write_text("pass\n")
        run_git(push_repo, "add", "env_test.py")
        run_git(push_repo, "commit", "-q", "-m", "env test commit")
        head_sha = git_head(push_repo)

        changes = push_repo / ".rlsbl" / "changes"
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "env test",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(push_repo, "add", ".rlsbl")
        run_git(push_repo, "commit", "-q", "-m", "changelog")

        ctx = _make_push_ctx(push_repo)

        captured_env = {}
        original_run = subprocess.run

        def mock_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "git" and "push" in cmd:
                # Capture the environment the push was called with
                env = kwargs.get("env")
                if env is not None:
                    captured_env.update(env)
                else:
                    # No explicit env passed -- inherits os.environ
                    captured_env["__inherited__"] = True
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            return original_run(cmd, *args, **kwargs)

        # Ensure RLSBL_RELEASE_PUSH is not in the current env
        monkeypatch.delenv("RLSBL_RELEASE_PUSH", raising=False)

        with patch("rlsbl.commands.push_cmd.subprocess.run", side_effect=mock_run):
            run_push(ctx, yes=True, quiet=True)

        # The push either inherited os.environ (no explicit env, so no
        # RLSBL_RELEASE_PUSH) or had an explicit env without it
        if "__inherited__" in captured_env:
            assert os.environ.get("RLSBL_RELEASE_PUSH") != "1"
        else:
            assert captured_env.get("RLSBL_RELEASE_PUSH") != "1"
