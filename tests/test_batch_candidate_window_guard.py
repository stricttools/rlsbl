"""The batch orchestrator's own candidate push is window-guarded, per member.

A batch publishes every member's release commits in ONE push, and the batch CI
gate then applies EACH member's own publish-gate filter to the resulting run.
So the push has to trigger every member's router job -- a member the push
window misses concludes ``skipped``, its publish gate refuses that check, and
the whole batch deadlocks after a full CI wait.

The per-member release path has refused an empty window from the diff since C5.
The batch orchestrator's own push went straight to ``push_if_needed`` with no
guard at all. It now runs the same guard once per member. Per member is the
point: the union of the members' filter patterns would pass the moment ONE
member's paths were touched, which is exactly the half-skipped batch this
whole two-pass design exists to prevent.
"""

import os
from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.commands.monorepo.batch_release import _publish_batch_candidate
from rlsbl.commands.release.execute import ReleaseCIError
from rlsbl.commands.release.release_state import (
    get_state_path,
    load_release_state,
    save_release_state,
)
from rlsbl.utils import run as real_run
from rlsbl.workspace import get_releasable_dir

from test_batch_main_as_candidate import (  # noqa: E402
    _setup_releasable_batch_workspace,
)


MEMBERS = (("alpha", "alpha-pkg"), ("beta", "beta-pkg"))


def _seed_member_states(root, bump_shas):
    """Write one in-progress state per member, as pass 1 leaves them.

    Returns the ``pending`` list ``_publish_batch_candidate`` consumes:
    ``(name, project_dir, state_path)`` per member.
    """
    pending = []
    for rel_name, project_name in MEMBERS:
        rel_dir = get_releasable_dir(str(root), rel_name)
        state_path = get_state_path(str(root), releasable_dir=rel_dir)
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        save_release_state(state_path, {
            "new_version": "1.0.1",
            "tag": f"{rel_name}@v1.0.1",
            "branch": "main",
            "registry": "npm",
            "monorepo_name": project_name,
            "releasable_name": rel_name,
            "release_commits": [bump_shas[rel_name]],
            "completed_steps": ["VERSION_BUMPED", "COMMITTED"],
        })
        pending.append((rel_name, os.path.join(str(root), rel_name), state_path))
    return pending


def _commit_touching(root, path, message):
    full = os.path.join(str(root), path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write("x\n")
    git(root, "add", path)
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def _run_candidate_push(root, pending, remote_head):
    """Call the batch candidate push with ``git ls-remote`` answering *remote_head*.

    Returns the ``push_if_needed`` mock so callers can assert whether the
    candidate ever reached the remote.
    """
    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "git" and args and args[0] == "ls-remote":
            return f"{remote_head}\trefs/heads/main\n"
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    with patch("rlsbl.commands.monorepo.batch_release.run",
               side_effect=fake_run), \
         patch("rlsbl.commands.release.push_if_needed") as pushed:
        _publish_batch_candidate(
            str(root), pending, {}, lambda m: None, pin_sha=None, trail=(),
        )
    return pushed


class TestBatchCandidateWindowGuard:

    def _staged(self, root):
        """A releasable batch whose two members each have a bump commit."""
        _setup_releasable_batch_workspace(root)
        bump_shas = {}
        for rel_name, _project_name in MEMBERS:
            bump_shas[rel_name] = _commit_touching(
                root, f"{rel_name}/package.json", f"{rel_name}@v1.0.1",
            )
        return bump_shas

    def test_a_window_covering_every_member_is_pushed(self, tmp_project):
        bump_shas = self._staged(tmp_project)
        pending = _seed_member_states(tmp_project, bump_shas)
        # The remote is behind BOTH bump commits: the window carries both.
        base = git(tmp_project, "rev-parse", "HEAD~2")

        pushed = _run_candidate_push(tmp_project, pending, remote_head=base)
        pushed.assert_called_once()

    def test_a_window_covering_only_one_member_is_refused(self, tmp_project):
        """The union-of-patterns shape: alpha matches, beta does not.

        A guard built on the union of the members' filters would pass here --
        alpha's paths ARE in the window -- and the batch would burn a full CI
        wait only to find beta's job skipped and its publish gate refusing.
        """
        bump_shas = self._staged(tmp_project)
        pending = _seed_member_states(tmp_project, bump_shas)
        # The remote already has both bumps; the only new commit is alpha's.
        base = git(tmp_project, "rev-parse", "HEAD")
        alpha_only = _commit_touching(
            tmp_project, "alpha/extra.txt", "alpha: follow-up",
        )
        assert alpha_only != base

        with patch("rlsbl.commands.release.push_if_needed") as pushed:
            with pytest.raises(ReleaseCIError) as exc:
                _run_candidate_push(tmp_project, pending, remote_head=base)
        detail = str(exc.value)
        assert "beta/**" in detail, (
            "the member whose CI the window cannot trigger must be named by "
            f"its filters; got: {detail}"
        )
        assert "beta@v1.0.1" in detail
        pushed.assert_not_called()

    def test_the_refusal_records_a_resumable_failure_on_that_member(
        self, tmp_project,
    ):
        """No version is burnt: the member stays resumable at the same one."""
        bump_shas = self._staged(tmp_project)
        pending = _seed_member_states(tmp_project, bump_shas)
        base = git(tmp_project, "rev-parse", "HEAD")
        _commit_touching(tmp_project, "alpha/extra.txt", "alpha: follow-up")

        with pytest.raises(ReleaseCIError):
            _run_candidate_push(tmp_project, pending, remote_head=base)

        beta_state = load_release_state(pending[1][2])
        assert beta_state["new_version"] == "1.0.1"
        assert "CI_VERIFIED" in beta_state.get("failed_steps", {})

        tags = git(tmp_project, "tag", "-l").split()
        assert "beta@v1.0.1" not in tags

    def test_nothing_is_pushed_before_the_guard_runs(self, tmp_project):
        """The refusal must precede the push, not follow it."""
        bump_shas = self._staged(tmp_project)
        pending = _seed_member_states(tmp_project, bump_shas)
        base = git(tmp_project, "rev-parse", "HEAD")
        _commit_touching(tmp_project, "alpha/extra.txt", "alpha: follow-up")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "git" and args and args[0] == "ls-remote":
                return f"{base}\trefs/heads/main\n"
            assert not (cmd == "git" and args and args[0] == "push"), (
                "the candidate must not be pushed before the window guard"
            )
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with patch("rlsbl.commands.monorepo.batch_release.run",
                   side_effect=fake_run), \
             patch("rlsbl.commands.release.push_if_needed") as pushed:
            with pytest.raises(ReleaseCIError):
                _publish_batch_candidate(
                    str(tmp_project), pending, {}, lambda m: None,
                    pin_sha=None, trail=(),
                )
        pushed.assert_not_called()

    def test_no_remote_head_leaves_the_guard_inert(self, tmp_project):
        """No before-SHA, no window to reason about: the CI gate decides."""
        bump_shas = self._staged(tmp_project)
        pending = _seed_member_states(tmp_project, bump_shas)

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "git" and args and args[0] == "ls-remote":
                return ""
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with patch("rlsbl.commands.monorepo.batch_release.run",
                   side_effect=fake_run), \
             patch("rlsbl.commands.release.push_if_needed") as pushed:
            _publish_batch_candidate(
                str(tmp_project), pending, {}, lambda m: None,
                pin_sha=None, trail=(),
            )
        pushed.assert_called_once()
