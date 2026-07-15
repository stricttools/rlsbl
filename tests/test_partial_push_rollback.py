"""Tests for partial push rollback bug.

When branch push succeeds but tag push fails, the release must NOT roll back
commits with `git reset --hard` -- those commits are already on the remote.
Rolling them back creates divergent local/remote state requiring manual
intervention.

See todo/partial-push-rollback-bug.md for the original bug report.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from pathlib import Path
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


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


def _setup_releasable_npm_project(repo):
    """Create a git repo with a tagged v1.0.0 release and an unreleased commit
    covered by an unreleased.jsonl entry. Ready for `rlsbl release patch`.
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    # Initial release: package.json @ 1.0.0
    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"]}) + "\n"
    )
    _git(repo, "add", "package.json", "CHANGELOG.md",
         ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    # Make an unreleased commit and cover it with a JSONL entry
    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**Add feature.** New feature available.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry")


class TestPartialPushRollback:
    """When branch push succeeds but tag push fails, git reset --hard must NOT
    be called because commits are already on the remote."""

    def test_no_reset_hard_when_branch_pushed_but_tag_fails(self, tmp_project):
        """Branch push succeeds, tag push fails: must NOT call git reset --hard.

        This is the red-green regression test for the partial-push-rollback bug.
        """
        _setup_releasable_npm_project(tmp_project)

        from rlsbl.commands.release import run_cmd
        from rlsbl.utils import run as real_run

        reset_hard_called = False
        tag_push_error = subprocess.CalledProcessError(
            1, ["git", "push", "origin", "v1.0.1"],
            stderr="error: failed to push tag",
        )

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            """Let all git commands through EXCEPT:
            - git push origin v1.0.1 (tag push) -> raise CalledProcessError
            - git reset --hard -> track the call, then run it
            - gh commands -> no-op
            """
            nonlocal reset_hard_called
            if cmd == "gh":
                return ""
            if cmd == "git" and args:
                # Track if git reset --hard is called
                if len(args) >= 2 and args[0] == "reset" and args[1] == "--hard":
                    reset_hard_called = True
                    # Still run it so the test doesn't break on subsequent operations
                    return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)
                # Tag push should fail
                if args[0] == "push" and len(args) >= 3 and args[1] == "origin":
                    # Check if this is a tag push (not branch push)
                    # Tag pushes have a tag name like "v1.0.1" as args[2]
                    if args[2].startswith("v"):
                        raise tag_push_error
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            # push_if_needed succeeds (branch push works)
            patch("rlsbl.commands.release.push_if_needed"),
            # Intercept run() to fail tag push and track reset --hard
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            with pytest.raises((SystemExit, subprocess.CalledProcessError)):
                run_cmd(
                    _rc(),
                    {"yes": True, "quiet": True},
                    ctx=ProjectContext(
                        project_root=Path("."),
                        workspace_root=None,
                        config={"private": False, "pipelines": {}},
                    ),
                )

        assert not reset_hard_called, (
            "git reset --hard must NOT be called when branch push succeeded "
            "but tag push failed -- the commits are already on the remote"
        )


def _is_tracked(repo, relpath):
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relpath],
        cwd=str(repo), capture_output=True, text=True,
    )
    return result.returncode == 0


def _porcelain(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _setup_rerelease_with_committed_finalize(repo):
    """Simulate a re-release where an earlier partial attempt already committed
    the finalize files for v1.0.1 (1.0.1.jsonl / 1.0.1.md) but never tagged and
    left package.json at 1.0.0 with the changelog entry still in
    unreleased.jsonl. On re-run, preflight passes (entry present in
    unreleased.jsonl) and the mutating phase fails at finalize_version --
    which refuses to overwrite the already-existing, tracked 1.0.1.jsonl. This
    is a genuine pre-TAGGED failure whose rollback must preserve the tracked
    finalize files.
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"]}) + "\n"
    )
    _git(repo, "add", "package.json", "CHANGELOG.md",
         ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

    # Unreleased feature commit, covered by an entry in unreleased.jsonl so
    # preflight (coverage + user-facing) passes.
    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(repo)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**Add feature.** New feature available.",
        "type": "feature",
    }
    (changes_dir / "unreleased.jsonl").write_text(json.dumps(entry) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry")

    # Earlier partial attempt: 1.0.1.jsonl / 1.0.1.md committed (tracked) but
    # never tagged. These are the finalize files the rollback must preserve.
    (changes_dir / "1.0.1.jsonl").write_text(json.dumps(entry) + "\n")
    (changes_dir / "1.0.1.md").write_text("## 1.0.1\n\n### Features\n- Add feature.\n")
    _git(repo, "add", ".rlsbl/changes/1.0.1.jsonl", ".rlsbl/changes/1.0.1.md")
    _git(repo, "commit", "-q", "-m", "chore: leftover finalize files from earlier attempt")


class TestPreTaggedReReleaseRollback:
    """Pre-TAGGED rollback must not destroy tracked finalize files, must not
    emit the (wrong, unreachable) force-push hint, and must point the user at
    fix-and-retry via `rlsbl release run`."""

    def test_rollback_preserves_tracked_finalize_and_no_forcepush_hint(
        self, tmp_project, capsys
    ):
        """A pre-TAGGED failure (finalize_version refuses to clobber the
        tracked 1.0.1.jsonl) rolls back. Cleanup must preserve the tracked
        finalize files, the tree must be byte-identical clean, no
        `--force-with-lease` hint may appear, and the fix-and-retry pointer
        must be present.
        """
        _setup_rerelease_with_committed_finalize(tmp_project)

        from rlsbl.commands.release import run_cmd
        from rlsbl.errors import RlsblError
        from rlsbl.utils import run as real_run

        pre_sha = _git_head(tmp_project)

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "gh":
                return ""
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            with pytest.raises((SystemExit, RlsblError, subprocess.CalledProcessError)):
                run_cmd(
                    _rc(),
                    {"yes": True, "quiet": False},
                    ctx=ProjectContext(
                        project_root=Path("."), workspace_root=None,
                        config={"private": False, "pipelines": {}},
                    ),
                )

        # Rollback landed us back at the pre-release HEAD.
        assert _git_head(tmp_project) == pre_sha, \
            "pre-TAGGED failure must reset to the pre-release commit"

        # Tracked finalize files survive and stay tracked.
        jsonl = tmp_project / ".rlsbl" / "changes" / "1.0.1.jsonl"
        md = tmp_project / ".rlsbl" / "changes" / "1.0.1.md"
        assert jsonl.exists() and md.exists(), \
            "tracked finalize files must not be deleted by cleanup"
        assert _is_tracked(tmp_project, ".rlsbl/changes/1.0.1.jsonl")
        assert _is_tracked(tmp_project, ".rlsbl/changes/1.0.1.md")

        # Byte-identical clean working tree (no ` D` entries, no orphans).
        assert _porcelain(tmp_project) == "", \
            "rollback must leave a byte-identical clean working tree"

        err = capsys.readouterr().err
        assert "force-with-lease" not in err, \
            "the unreachable force-push hint must be gone"
        assert "push --force" not in err
        assert "rlsbl release run" in err, \
            "rollback must point the user at fix-and-retry via rlsbl release run"
        assert "residual" not in err.lower(), \
            "a clean rollback must not emit the residual-leftover warning"


def _tag_exists(repo, tag):
    result = subprocess.run(
        ["git", "tag", "-l", tag],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip())


class TestPostTaggedPushResumable:
    """Post-TAGGED push failures must be classified RESUMABLE, not rolled back.

    Once the release is TAGGED and its finalized changelog / release-file
    artifacts are on disk, ANY push failure (branch push OR tag push, plain
    failure OR timeout) is the canonical resumable state. The executor must
    skip the entire rollback family (no `git reset --hard`, no tag deletion,
    no artifact cleanup, no state clearing), record a failed PUSHED marker,
    and print the resume command.

    Red-green regression for the push-timeout resumable-classification bug:
    a transient branch-push stall AFTER tagging previously triggered a
    destructive rollback (classification was keyed on `branch_pushed` instead
    of `TAGGED`), destroying exactly the state `release resume` needs.

    Two timeout-surfacing forms are covered:
      1. Branch push timeout -> `push_if_needed` raises `GitError` ("timed out").
      2. Tag push timeout -> raw `run()` raises `subprocess.TimeoutExpired`,
         which the executor converts to `GitError` at the call site.
    """

    def _assert_resumable_state(self, repo, pre_sha):
        """Common post-conditions: tag + commits + finalized files + state
        all preserved, PUSHED recorded as a failed step."""
        from rlsbl.commands.release.release_state import (
            get_state_path, load_release_state,
        )

        assert _tag_exists(repo, "v1.0.1"), \
            "tag must be preserved after a post-TAGGED push failure"
        assert _git_head(repo) != pre_sha, \
            "release commits must NOT be rolled back post-TAGGED"

        state = load_release_state(get_state_path(str(repo)))
        assert state is not None, "in-progress.json must be preserved"
        assert "TAGGED" in state["completed_steps"]
        assert "PUSHED" not in state["completed_steps"]
        assert "PUSHED" in state.get("failed_steps", {}), \
            "a failed PUSHED marker must be recorded"

        assert (repo / ".rlsbl" / "changes" / "1.0.1.jsonl").exists(), \
            "finalized changelog file must remain on disk"

    def test_branch_push_timeout_after_tag_is_resumable(self, tmp_project, capsys):
        """Form 1: branch push times out (GitError) after the tag is created."""
        _setup_releasable_npm_project(tmp_project)

        from rlsbl.commands.release import run_cmd
        from rlsbl.errors import GitError
        from rlsbl.utils import run as real_run

        pre_sha = _git_head(tmp_project)
        reset_hard_called = False

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            nonlocal reset_hard_called
            if cmd == "gh":
                return ""
            if (cmd == "git" and args and len(args) >= 2
                    and args[0] == "reset" and args[1] == "--hard"):
                reset_hard_called = True
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        branch_timeout = GitError(
            "Push timed out after 120s — remote state may be inconsistent. "
            "Check with: git push --dry-run"
        )

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.push_if_needed", side_effect=branch_timeout),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            with pytest.raises(GitError):
                run_cmd(
                    _rc(),
                    {"yes": True, "quiet": False},
                    ctx=ProjectContext(
                        project_root=Path("."), workspace_root=None,
                        config={"private": False, "pipelines": {}},
                    ),
                )

        assert not reset_hard_called, \
            "git reset --hard must NOT run for a post-TAGGED branch-push timeout"
        self._assert_resumable_state(tmp_project, pre_sha)

        captured = capsys.readouterr()
        assert "rlsbl release resume" in captured.err
        assert "RLSBL_PUSH_TIMEOUT" in captured.err, \
            "a timeout failure must suggest raising RLSBL_PUSH_TIMEOUT"

    def test_tag_push_raw_timeout_after_tag_is_resumable(self, tmp_project, capsys):
        """Form 2: tag push raises raw subprocess.TimeoutExpired (branch OK)."""
        _setup_releasable_npm_project(tmp_project)

        from rlsbl.commands.release import run_cmd
        from rlsbl.errors import GitError
        from rlsbl.utils import run as real_run

        pre_sha = _git_head(tmp_project)
        reset_hard_called = False

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            nonlocal reset_hard_called
            if cmd == "gh":
                return ""
            if cmd == "git" and args:
                if (len(args) >= 2 and args[0] == "reset" and args[1] == "--hard"):
                    reset_hard_called = True
                # Raw tag push times out (tag refs start with "v").
                if (args[0] == "push" and len(args) >= 3
                        and args[1] == "origin" and args[2].startswith("v")):
                    raise subprocess.TimeoutExpired(
                        cmd=["git", "push", "origin", args[2]], timeout=timeout,
                    )
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            # Branch push succeeds (no-op) -> branch_pushed becomes True.
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            # Raw TimeoutExpired is converted to GitError at the call site.
            with pytest.raises(GitError):
                run_cmd(
                    _rc(),
                    {"yes": True, "quiet": False},
                    ctx=ProjectContext(
                        project_root=Path("."), workspace_root=None,
                        config={"private": False, "pipelines": {}},
                    ),
                )

        assert not reset_hard_called, \
            "git reset --hard must NOT run for a post-TAGGED tag-push timeout"
        self._assert_resumable_state(tmp_project, pre_sha)

        captured = capsys.readouterr()
        assert "rlsbl release resume" in captured.err
        assert "RLSBL_PUSH_TIMEOUT" in captured.err
