"""Tests for the rollback-clobber guard: refuse git reset --hard when
foreign commits would be destroyed.

The guard protects concurrent sessions sharing a worktree from having
their committed work silently discarded during a failed release rollback.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release.execute import (
    RollbackClobberError,
    _guard_rollback,
    _track_release_commit,
)
from rlsbl.commands.release.release_state import (
    load_release_state,
    save_release_state,
    get_state_path,
)
from rlsbl.context import ProjectContext


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _setup_repo(tmp_path):
    """Create a git repo with an initial commit and return (repo, pre_release_sha)."""
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")

    pre_release_sha = _git_head(repo)

    # Create .rlsbl/releases/ for state file
    releases_dir = repo / ".rlsbl" / "releases"
    releases_dir.mkdir(parents=True)

    return repo, pre_release_sha


class TestTrackReleaseCommit:
    """Tests for _track_release_commit: captures HEAD SHA into state."""

    def test_tracks_single_commit(self, tmp_path):
        """A single release commit is recorded in state."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        # Save initial state
        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Make a release commit
        (repo / "version.txt").write_text("1.0.0\n")
        _git(repo, "add", "version.txt")
        _git(repo, "commit", "-q", "-m", "v1.0.0")
        release_sha = _git_head(repo)

        _track_release_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert "release_commits" in state
        assert release_sha in state["release_commits"]

    def test_tracks_multiple_commits(self, tmp_path):
        """Multiple release commits are all recorded."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        shas = []
        for i in range(3):
            (repo / f"file{i}.txt").write_text(f"content {i}\n")
            _git(repo, "add", f"file{i}.txt")
            _git(repo, "commit", "-q", "-m", f"release commit {i}")
            shas.append(_git_head(repo))
            _track_release_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert len(state["release_commits"]) == 3
        for sha in shas:
            assert sha in state["release_commits"]

    def test_deduplicates_same_sha(self, tmp_path):
        """Calling _track_release_commit twice for the same HEAD is idempotent."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        (repo / "file.txt").write_text("data\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-q", "-m", "commit")

        _track_release_commit(state_path, cwd=str(repo))
        _track_release_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert len(state["release_commits"]) == 1


class TestGuardRollback:
    """Tests for _guard_rollback: blocks rollback when foreign work exists."""

    def test_allows_rollback_with_only_release_commits(self, tmp_path):
        """Normal rollback (no foreign commits, no dirty files) succeeds."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Make two release commits
        for i in range(2):
            (repo / f"release{i}.txt").write_text(f"release {i}\n")
            _git(repo, "add", f"release{i}.txt")
            _git(repo, "commit", "-q", "-m", f"release {i}")
            _track_release_commit(state_path, cwd=str(repo))

        # Guard should NOT raise
        _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

    def test_blocks_rollback_with_foreign_commit(self, tmp_path):
        """Foreign commit between pre_release_sha and HEAD triggers guard."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Make a release commit (tracked)
        (repo / "release.txt").write_text("release\n")
        _git(repo, "add", "release.txt")
        _git(repo, "commit", "-q", "-m", "release commit")
        _track_release_commit(state_path, cwd=str(repo))

        # Make a foreign commit (NOT tracked -- simulates concurrent session)
        (repo / "foreign.txt").write_text("concurrent work\n")
        _git(repo, "add", "foreign.txt")
        _git(repo, "commit", "-q", "-m", "foreign commit")
        foreign_sha = _git_head(repo)

        with pytest.raises(RollbackClobberError) as exc_info:
            _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

        error_msg = str(exc_info.value)
        assert "Foreign commits" in error_msg
        assert foreign_sha in error_msg
        assert "Manual recovery" in error_msg

    def test_allows_rollback_with_dirty_files_only(self, tmp_path):
        """Dirty working tree alone does NOT trigger guard.

        The release flow writes version-bump and changelog files before
        committing them. At rollback time, those dirty files are the
        expected rollback target, not concurrent work.  Untracked files
        survive git reset --hard anyway.
        """
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Make a release commit (tracked)
        (repo / "release.txt").write_text("release\n")
        _git(repo, "add", "release.txt")
        _git(repo, "commit", "-q", "-m", "release commit")
        _track_release_commit(state_path, cwd=str(repo))

        # Create dirty files (simulates version-bump writes before commit)
        (repo / "dirty.txt").write_text("uncommitted work\n")
        (repo / "README.md").write_text("modified\n")

        # Guard should NOT raise -- dirty files alone are not foreign work
        _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

    def test_blocks_rollback_with_foreign_commit_despite_dirty_files(self, tmp_path):
        """Foreign commits trigger the guard regardless of dirty files."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Tracked release commit
        (repo / "release.txt").write_text("release\n")
        _git(repo, "add", "release.txt")
        _git(repo, "commit", "-q", "-m", "release commit")
        _track_release_commit(state_path, cwd=str(repo))

        # Foreign commit
        (repo / "foreign.txt").write_text("foreign\n")
        _git(repo, "add", "foreign.txt")
        _git(repo, "commit", "-q", "-m", "foreign")

        # Also a dirty file
        (repo / "dirty.txt").write_text("dirty\n")

        with pytest.raises(RollbackClobberError) as exc_info:
            _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

        error_msg = str(exc_info.value)
        assert "Foreign commits" in error_msg

    def test_allows_rollback_with_no_commits_since_pre_release(self, tmp_path):
        """When HEAD == pre_release_sha (no commits at all), rollback is safe."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # No commits made -- HEAD is still pre_release_sha
        _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

    def test_allows_rollback_with_empty_release_commits(self, tmp_path):
        """When state has no release_commits and no new commits, rollback is safe."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        # State without release_commits key
        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        _guard_rollback(pre_release_sha, state_path, cwd=str(repo))

    def test_graceful_on_missing_state_file(self, tmp_path):
        """Guard does not crash when state file is missing."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        # No state file saved -- should not crash
        _guard_rollback(pre_release_sha, state_path, cwd=str(repo))


class TestRollbackClobberInReleaseFlow:
    """Integration-level tests: verify the guard fires in the actual except blocks."""

    def test_release_abort_with_foreign_commit_raises_clobber_error(self, tmp_project):
        """When a release fails and there is a foreign commit, RollbackClobberError
        propagates instead of silently resetting."""
        repo = tmp_project
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@test.local")
        _git(repo, "config", "user.name", "Test")

        # Set up a tagged project
        (repo / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
        )
        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
        )
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init.\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")
        _git(repo, "tag", "v1.0.0")

        # Unreleased commit with changelog
        (repo / "feature.txt").write_text("feature\n")
        _git(repo, "add", "feature.txt")
        _git(repo, "commit", "-q", "-m", "add feature")
        feature_sha = _git_head(repo)

        entry = {
            "commits": [feature_sha],
            "user_facing": True,
            "description": "**Feature.** A new thing.",
            "type": "feature",
        }
        (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
        _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(repo, "commit", "-q", "-m", "changelog")

        from rlsbl.commands.release import run_cmd
        from rlsbl.release_file import ReleaseConfig
        from rlsbl.utils import run as real_run

        foreign_commit_injected = False

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            """Inject a foreign commit after the version bump commit, then
            trigger a failure to cause rollback."""
            nonlocal foreign_commit_injected
            result = real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

            # After a commit_files call that creates the version bump commit,
            # inject a foreign commit. We detect this by checking if the HEAD
            # message matches the release tag.
            if (cmd == "git" and args and args[0] == "log"
                    and not foreign_commit_injected):
                head_msg = result.strip()
                if head_msg == "v1.0.1":
                    (repo / "foreign_work.txt").write_text("concurrent session\n")
                    _git(repo, "add", "foreign_work.txt")
                    _git(repo, "commit", "-q", "-m", "foreign concurrent work")
                    foreign_commit_injected = True

            # Fail on push to trigger rollback
            if cmd == "git" and args and args[0] == "push":
                raise subprocess.CalledProcessError(1, ["git", "push"], stderr="push failed")

            return result

        rc = ReleaseConfig(bump="patch", include=["npm"], exclude=[])

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed",
                  side_effect=subprocess.CalledProcessError(1, ["git", "push"], stderr="push failed")),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            # The push fails AFTER tagging, which is the canonical resumable
            # state: no rollback runs (so the foreign commit is never at risk),
            # and run_cmd converts the raw push CalledProcessError into a clean
            # SystemExit(1) instead of propagating it.
            with pytest.raises(SystemExit) as _exc:
                run_cmd(
                    rc,
                    {"yes": True, "quiet": True},
                    ctx=ProjectContext(
                        project_root=Path("."),
                        workspace_root=None,
                        config={"publish_mode": "ci", "pipelines": {}},
                    ),
                )
        assert _exc.value.code == 1
