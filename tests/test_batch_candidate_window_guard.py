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

import json
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


def _seed_stranded_member_states(root, bump_shas, candidate_sha):
    """One in-progress state per member, as a STRANDED resume leaves them.

    The distinguishing fact against :func:`_seed_member_states`: an earlier
    attempt already pushed a candidate (``BRANCH_PUSHED`` is recorded and
    ``candidate_sha`` names the commit that reached the remote), and CI came
    back red. The operator fixed forward outside every member's paths.
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
            "candidate_sha": candidate_sha,
            "completed_steps": [
                "VERSION_BUMPED", "COMMITTED", "BRANCH_PUSHED",
            ],
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


class TestStrandedResumeDispatchesRunAllItself:
    """The guard used to deadlock its own remedy.

    A batch whose members were already published as a candidate, went red, and
    were fixed forward outside every member's paths hit the guard BEFORE the
    push -- and the guard's prescribed remedy (dispatch the router at the
    candidate with ``run_all=true``) needs that fix commit ON THE REMOTE to be
    dispatchable at all. Refusing before the push made the remedy unreachable:
    the release could neither proceed nor be repaired.

    So for exactly that shape -- a push is owed AND a prior candidate was
    already published -- rlsbl pushes the candidate and dispatches the run_all
    workflow itself, then gates on the dispatched run. The refusal stays for the
    fresh case, where an empty window is a configuration defect rather than an
    honestly narrow fix-forward.
    """

    def _stranded(self, tmp_project):
        """Both members published, red, then a fix-forward outside every watch."""
        _setup_releasable_batch_workspace(tmp_project)
        wf_dir = tmp_project / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci-router.yml").write_text(
            "name: CI Router\non:\n  push:\n    branches: [main]\n"
            "  workflow_dispatch:\n    inputs:\n      run_all:\n"
            "        type: boolean\njobs: {}\n"
        )
        git(tmp_project, "add", ".github/workflows/ci-router.yml")
        git(tmp_project, "commit", "-q", "-m", "ci: router")
        bump_shas = {}
        for rel_name, _project_name in MEMBERS:
            bump_shas[rel_name] = _commit_touching(
                tmp_project, f"{rel_name}/package.json", f"{rel_name}@v1.0.1",
            )
        published = git(tmp_project, "rev-parse", "HEAD")
        pending = _seed_stranded_member_states(
            tmp_project, bump_shas, published,
        )
        fix_sha = _commit_touching(
            tmp_project, "docs/notes.md", "docs: fix forward",
        )
        return pending, published, fix_sha

    @staticmethod
    def _gh_recorder(head_sha, calls):
        def fake_gh(args, **kwargs):
            calls.append(list(args))
            if list(args[:2]) == ["run", "list"]:
                return json.dumps([{
                    "databaseId": 4242,
                    "headSha": head_sha,
                    "status": "queued",
                    "workflowName": "CI Router",
                    "event": "workflow_dispatch",
                }])
            return ""
        return fake_gh

    def test_the_candidate_is_pushed_and_run_all_is_dispatched(self, tmp_project):
        pending, published, fix_sha = self._stranded(tmp_project)
        calls = []

        with patch("rlsbl.commands.watch.run_gh",
                   side_effect=self._gh_recorder(fix_sha, calls)):
            pushed = _run_candidate_push(
                tmp_project, pending, remote_head=published,
            )

        pushed.assert_called_once()
        assert any(
            call[:2] == ["workflow", "run"]
            and "ci-router.yml" in call
            and "run_all=true" in call
            for call in calls
        ), (
            f"the release must dispatch the run_all workflow itself; gh calls: "
            f"{calls}"
        )

    def test_the_dispatch_is_correlated_to_the_pushed_candidate(self, tmp_project):
        """A dispatched run for some other commit proves nothing: fail closed."""
        pending, published, _fix = self._stranded(tmp_project)
        calls = []

        with patch("rlsbl.commands.watch.run_gh",
                   side_effect=self._gh_recorder("f" * 40, calls)), \
             patch("rlsbl.commands.watch.RUN_ALL_DISPATCH_ATTEMPTS", 2), \
             patch("rlsbl.commands.watch.RUN_ALL_DISPATCH_INTERVAL", 0):
            with pytest.raises(Exception) as exc:
                _run_candidate_push(
                    tmp_project, pending, remote_head=published,
                )
        assert "run_all" in str(exc.value)

    def test_no_resumable_failure_is_recorded(self, tmp_project):
        pending, published, fix_sha = self._stranded(tmp_project)
        calls = []

        with patch("rlsbl.commands.watch.run_gh",
                   side_effect=self._gh_recorder(fix_sha, calls)):
            _run_candidate_push(tmp_project, pending, remote_head=published)

        for _name, _dir, state_path in pending:
            state = load_release_state(state_path)
            assert "CI_VERIFIED" not in (state.get("failed_steps") or {})
            assert "BRANCH_PUSHED" in state["completed_steps"]
