"""Tests for partial push rollback bug.

When branch push succeeds but tag push fails, the release must NOT roll back
commits with `git reset --hard` -- those commits are already on the remote.
Rolling them back creates divergent local/remote state requiring manual
intervention.

See todo/partial-push-rollback-bug.md for the original bug report.
"""

import json
import os
import subprocess
from unittest.mock import patch, call, MagicMock

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
