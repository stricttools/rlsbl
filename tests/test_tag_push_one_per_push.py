"""Every release tag reaches the remote in a push of its own, primary first.

GitHub creates no events at all when more than three tags arrive in one push:
not for the tags, not for workflows listening to them, not for webhooks. The
probe measured it with rlsbl's own shape (a primary tag plus Go module tags such
as ``draw/v1.0.0`` and ``draw/cmd/v1.0.0``): four tags in one push produced
nothing, and the same tags pushed one per push produced every event. So the
release pushes its primary tag alone, then each extra tag alone. The same holds
for the in-handler retry after a failed tag push.
"""

import dataclasses
import subprocess
from unittest.mock import patch

from rlsbl.commands.release import run_cmd
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig
from rlsbl.targets import TARGETS
from rlsbl.utils import run as real_run

from test_post_push_failure_state import _setup_npm_project

EXTRA = ("pkg/a/v1.0.1", "pkg/b/v1.0.1", "pkg/c/v1.0.1")


def _with_extra_tags(original):
    def expected_refs(self, version, ctx):
        refs = original(self, version, ctx)
        return dataclasses.replace(refs, companions=EXTRA)
    return expected_refs


def _tag_pushes(calls):
    pushes = []
    for args in calls:
        rest = [a for a in args if a != "--no-verify"]
        if rest[:2] == ["push", "origin"] and "refs/heads" not in " ".join(rest):
            pushes.append(rest[2:])
    return pushes


def _release(repo, fake_run):
    target_cls = type(TARGETS["npm"])
    with (
        patch.object(target_cls, "expected_refs",
                     _with_extra_tags(target_cls.expected_refs)),
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.validate_gh_push_access"),
        patch("rlsbl.commands.release.validate_branch_and_remote",
              return_value="main"),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.resolve_tag_push_plan", return_value=True),
        patch("rlsbl.commands.release.execute.time.sleep"),
        patch("rlsbl.commands.release.run", side_effect=fake_run),
        patch("rlsbl.commands.release.run_gh", return_value=""),
    ):
        run_cmd(
            ReleaseConfig(bump="patch", include=["npm"], exclude=[],
                          description="test release"),
            {"quiet": True},
            ctx=ProjectContext(
                project_root=repo, workspace_root=None,
                config={"publish_mode": "ci", "pipelines": {}},
            ),
        )


def test_the_primary_tag_goes_first_and_every_tag_alone(mock_git_repo):
    _setup_npm_project(mock_git_repo)
    calls = []

    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "git" and args and args[0] == "push":
            calls.append(list(args))
            return ""
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    _release(mock_git_repo, fake_run)

    assert _tag_pushes(calls) == [["v1.0.1"], *[[t] for t in EXTRA]]


def test_the_retry_after_a_failed_tag_push_also_pushes_one_tag_per_push(
    mock_git_repo,
):
    _setup_npm_project(mock_git_repo)
    calls = []

    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "git" and args and args[0] == "push":
            calls.append(list(args))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(
                    1, ["git", "push"], stderr="fatal: transient network blip",
                )
            return ""
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    _release(mock_git_repo, fake_run)

    pushes = _tag_pushes(calls)
    assert pushes[0] == ["v1.0.1"], "the primary tag is pushed first, alone"
    assert pushes[1:] == [["v1.0.1"], *[[t] for t in EXTRA]], (
        "the retry starts again from the primary tag and pushes each tag alone"
    )
    assert all(len(p) == 1 for p in pushes)


def test_undo_deletes_one_tag_per_push(tmp_project, monkeypatch):
    """Four or more deletions in one push create no delete events either, so
    ``rlsbl release undo`` deletes each tag in its own push. The origin records
    how many refs every push it receives updates."""
    import os
    from pathlib import Path

    from rlsbl.context import create_context
    from test_undo_releasable import (
        _git,
        _run_undo,
        _setup_released_releasable_workspace,
    )

    core = _setup_released_releasable_workspace(tmp_project)
    extra = ["core/a/v1.0.1", "core/b/v1.0.1", "core/c/v1.0.1"]
    for tag in extra:
        _git(tmp_project, "tag", tag)
        _git(tmp_project, "push", "-q", "origin", tag)

    origin = _git(tmp_project, "remote", "get-url", "origin").strip()
    log = Path(str(tmp_project)).parent / "pushes.log"
    hook = Path(origin) / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nwc -l >> '{log}'\n")
    os.chmod(hook, 0o755)

    monkeypatch.chdir(core)
    ctx = create_context(Path(str(core)), workspace_root=Path(str(tmp_project)))
    _run_undo(
        ctx, flags={"version": "1.0.1"},
        extra_patches=[patch("rlsbl.commands.undo._plan_companion_tags",
                             return_value=extra)],
    )

    tags_left = _git(tmp_project, "ls-remote", "--tags", "origin")
    assert not [t for t in extra if t in tags_left]
    counts = [int(line) for line in log.read_text().split()]
    assert counts and all(n == 1 for n in counts), counts
