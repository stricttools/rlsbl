"""Tests for the pre-push empty-candidate-window guard.

The generated monorepo CI router gates each project's job on a
dorny/paths-filter computed against the PUSH's own before-SHA. A push whose
diff matches none of a project's patterns leaves that project's job `skipped`,
and the publish gate refuses a skipped check -- correctly, since a skipped job
proves nothing about the commit. The release then deadlocks on a tag that can
never publish.

Reaching that verdict through the CI gate costs a full CI cycle. The window is
computable from the diff before the push, so it is refused there instead.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.commands.release.execute import _release_router_filters
from rlsbl.router_filters import matches_filter, matches_pattern
from rlsbl.commands.release.release_state import (
    get_state_path,
    load_release_state,
    save_release_state,
)
from rlsbl.workspace import get_releasable_dir

from test_representative_write_elimination import (  # noqa: E402
    _git,
    _git_head,
    _release_patches,
    _rc,
    _setup_releasable_workspace,
)


class TestRouterPatternMatching:
    """The shapes the router emits, matched the way picomatch does.

    The exhaustive conformance evidence is tests/test_router_filters.py, which
    replays verdicts captured from the real action. These are the cases the
    guard itself depends on.
    """

    @pytest.mark.parametrize("path,expected", [
        ("packages/core/package.json", True),
        ("packages/core/src/deep/file.ts", True),
        ("packages/core", True),
        ("packages/coreutils/x.ts", False),
        ("packages/other/package.json", False),
    ])
    def test_directory_globstar(self, path, expected):
        assert matches_pattern(path, "packages/core/**") is expected

    @pytest.mark.parametrize("path,expected", [
        (".rlsbl-monorepo/releasables/alpha/CHANGELOG.md", True),
        (".rlsbl-monorepo/releasables/beta/CHANGELOG.md", False),
    ])
    def test_exact_artifact_path(self, path, expected):
        pattern = ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md"
        assert matches_pattern(path, pattern) is expected

    def test_the_root_members_excludes_are_honoured(self):
        """A filter read as a whole, not as independent patterns.

        Reading each pattern on its own and OR-ing the results is how the
        simulation used to answer, and it reports a match for exactly the
        paths the action excludes.
        """
        patterns = ["**", "!packages/core/**"]
        assert matches_filter("README.md", patterns)
        assert not matches_filter("packages/core/src/index.ts", patterns)


class TestReleaseRouterFilters:
    """The guard asks for the same project set the CI gate demands."""

    def test_releasable_members_and_finalize_artifact(self, tmp_project):
        _setup_releasable_workspace(tmp_project)
        per_project = _release_router_filters(str(tmp_project), "core", "alpha")
        patterns = {p for _name, filt in per_project for p in filt}
        assert "packages/core/**" in patterns
        assert ".rlsbl-monorepo/releasables/alpha/CHANGELOG.md" in patterns

    def test_each_project_keeps_its_own_filter(self, tmp_project):
        """Filters are returned per project so one member's excludes cannot
        answer for another member's territory."""
        _setup_releasable_workspace(tmp_project)
        per_project = _release_router_filters(str(tmp_project), "core", "alpha")
        assert all(isinstance(name, str) and filt for name, filt in per_project)


def _prepare_resumable_candidate(root, core, unrelated_path="docs/notes.md",
                                 published=True):
    """Stage the resumed-sibling shape.

    The release's version-bump commit is ALREADY the remote head (a previous
    attempt pushed it as the candidate and CI came back red). The operator
    fixed forward -- but the fix touches somebody else's paths, so the new
    push window no longer contains anything of this project's.

    ``published=False`` drops the ``BRANCH_PUSHED`` marker and the recorded
    candidate: an attempt that committed but never published one. That is the
    discriminator the guard branches on -- an empty window with no prior
    published candidate is a configuration defect and stays a hard error, while
    the published shape is an honestly narrow fix-forward that rlsbl pushes and
    dispatches ``run_all`` for.
    """
    pkg = json.loads((core / "package.json").read_text())
    pkg["version"] = "1.0.1"
    (core / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    _git(root, "add", "packages/core/package.json")
    _git(root, "commit", "-q", "-m", "alpha@v1.0.1")
    bump_sha = _git_head(root)

    unrelated = root / unrelated_path
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("unrelated\n")
    _git(root, "add", unrelated_path)
    _git(root, "commit", "-q", "-m", "docs: unrelated fix")

    rel_dir = get_releasable_dir(str(root), "alpha")
    state_path = get_state_path(str(root), releasable_dir=rel_dir)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    save_release_state(state_path, {
        "new_version": "1.0.1",
        "tag": "alpha@v1.0.1",
        "branch": "main",
        "registry": "npm",
        "monorepo_name": "core",
        "releasable_name": "alpha",
        "commit_msg": "alpha@v1.0.1",
        "description": "",
        "context": "",
        "include": ["npm"],
        "exclude": [],
        "preid": "",
        "blog": False,
        "completed_steps": [
            "VERSION_BUMPED", "COMMITTED", "SNAPSHOT_REGENERATED",
            *(["BRANCH_PUSHED"] if published else []),
        ],
        "release_commits": [bump_sha],
        **({"candidate_sha": bump_sha} if published else {}),
    })
    return state_path, bump_sha


def _run_resume(root, core, remote_head, extra_patches=()):
    """Resume with `git ls-remote` answering with *remote_head*."""
    from rlsbl.commands.release import resume_cmd
    from rlsbl.context import create_context
    from rlsbl.utils import run as real_run

    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "gh":
            return ""
        if cmd == "git" and args and args[0] == "push":
            return ""
        if cmd == "git" and args and args[0] == "fetch":
            return ""
        if cmd == "git" and args and args[0] == "ls-remote":
            return f"{remote_head}\trefs/heads/main\n"
        if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
                and any("origin/" in a for a in args)):
            return "0"
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    rel_dir = get_releasable_dir(str(root), "alpha")
    state_path = get_state_path(str(root), releasable_dir=rel_dir)
    ctx = create_context(Path(str(core)), workspace_root=Path(str(root)))
    patches = [
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=fake_run),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
        *extra_patches,
    ]
    for p in patches:
        p.start()
    try:
        resume_cmd(
            load_release_state(state_path),
            {"quiet": True, "skip-lock": True},
            ctx=ctx,
        )
    finally:
        for p in patches:
            p.stop()


class TestEmptyWindowRefusedBeforeTheCiWait:
    """No candidate was ever published, so an empty window is a config defect.

    The workspace here HAS a generated router, so the refusal is the guard's
    verdict on the shape rather than an artifact of having nothing to dispatch.
    """

    def _staged(self, tmp_project):
        core = _setup_releasable_workspace(
            tmp_project, root_workflows=("ci-router.yml",),
        )
        return core, _prepare_resumable_candidate(
            tmp_project, core, published=False,
        )

    def test_an_unpublished_candidate_window_is_refused(self, tmp_project, capsys):
        core, (_state_path, bump_sha) = self._staged(tmp_project)

        waits = []
        with patch(
            "rlsbl.commands.release.wait_for_ci_green",
            side_effect=lambda *a, **kw: waits.append(a),
        ):
            with pytest.raises(SystemExit) as exc:
                _run_resume(tmp_project, core, remote_head=bump_sha)

        assert exc.value.code == 1
        assert waits == [], (
            "the guard must refuse BEFORE burning a CI wait"
        )
        err = capsys.readouterr().err
        assert "packages/core/**" in err
        assert "docs/notes.md" in err
        assert "rlsbl release resume" in err

    def test_nothing_was_pushed(self, tmp_project):
        core, (_state_path, bump_sha) = self._staged(tmp_project)

        with patch("rlsbl.commands.release.push_if_needed") as pushed:
            with pytest.raises(SystemExit):
                _run_resume(tmp_project, core, remote_head=bump_sha,
                            extra_patches=(patch(
                                "rlsbl.commands.release.wait_for_ci_green",
                                side_effect=AssertionError("no CI wait"),
                            ),))
        pushed.assert_not_called()

    def test_no_tag_was_created(self, tmp_project):
        core, (_state_path, bump_sha) = self._staged(tmp_project)

        with pytest.raises(SystemExit):
            _run_resume(tmp_project, core, remote_head=bump_sha)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "", "the version must not be burnt"


class TestWindowThatDoesTriggerCiIsAllowed:

    def test_a_window_containing_the_version_bump_passes(self, tmp_project):
        """The normal shape: the push window carries the bump commit.

        Same fixture as the refusal tests, with the remote head one commit
        further BACK -- so the window contains the version bump under
        ``packages/core/`` and the router would run the project's job.
        """
        core = _setup_releasable_workspace(tmp_project)
        base_sha = _git_head(tmp_project)
        state_path, _bump = _prepare_resumable_candidate(tmp_project, core)

        _run_resume(tmp_project, core, remote_head=base_sha)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "alpha@v1.0.1", "the release must run to completion"
        assert not os.path.exists(state_path)

    def test_a_standalone_repo_is_not_guarded(self, tmp_project):
        """No router, no paths filter: every push runs the whole CI."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        # monorepo_root/monorepo_name absent -> inert, whatever the diff says.
        _guard_empty_candidate_window(
            candidate_sha="a" * 40, remote_head="b" * 40, needs_push=True,
            state_path="/nonexistent/in-progress.json",
            monorepo_root=None, monorepo_name=None, releasable_name=None,
            version="1.0.1", tag="v1.0.1", branch="main",
            cwd=str(tmp_project), log=lambda m: None,
        )

    def test_no_remote_head_is_not_guarded(self, tmp_project):
        """A branch with no remote head has no before-SHA to reason about."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        _setup_releasable_workspace(tmp_project)
        _guard_empty_candidate_window(
            candidate_sha="a" * 40, remote_head=None, needs_push=True,
            state_path="/nonexistent/in-progress.json",
            monorepo_root=str(tmp_project), monorepo_name="core",
            releasable_name="alpha",
            version="1.0.1", tag="alpha@v1.0.1", branch="main",
            cwd=str(tmp_project), log=lambda m: None,
        )


class TestStrandedResumeDispatchesRunAllItself:
    """The single-release path's twin of the batch orchestrator's fix.

    `rlsbl release run` on one member reaches the SAME guard, from
    ``phase_a``'s GUARD_CANDIDATE_WINDOW step, and deadlocked the same way: the
    refusal fired before the push, while its own prescribed remedy (dispatch
    the router at the candidate with ``run_all=true``) needs the fix commit on
    the remote to be dispatchable at all.

    On a resume whose candidate was already published once, the release now
    pushes the candidate and dispatches the router itself, then gates on the
    dispatched run. The refusal stays for the fresh case.
    """

    def _staged(self, tmp_project):
        """The stranded shape, in a workspace that HAS a generated router."""
        core = _setup_releasable_workspace(
            tmp_project, root_workflows=("ci-router.yml",),
        )
        _state_path, bump_sha = _prepare_resumable_candidate(tmp_project, core)
        return core, bump_sha

    @staticmethod
    def _gh_recorder(head_sha, calls):
        def fake_gh(args, **kwargs):
            calls.append(list(args))
            if list(args[:2]) == ["run", "list"]:
                return json.dumps([{
                    "databaseId": 77,
                    "headSha": head_sha,
                    "status": "queued",
                    "workflowName": "CI Router",
                }])
            return ""
        return fake_gh

    def test_the_candidate_is_pushed_and_run_all_is_dispatched(self, tmp_project):
        core, bump_sha = self._staged(tmp_project)
        head = _git_head(tmp_project)
        calls = []
        # Through extra_patches: _run_resume patches the same name itself, and
        # its patch is started last, so a plain nested one would be shadowed.
        pushed = MagicMock()

        with patch("rlsbl.commands.watch.run_gh",
                   side_effect=self._gh_recorder(head, calls)):
            _run_resume(
                tmp_project, core, remote_head=bump_sha,
                extra_patches=(
                    patch("rlsbl.commands.release.push_if_needed", pushed),
                ),
            )

        # The FIRST push is the candidate (the later one carries the
        # finalization commits and the tags).
        assert pushed.call_args_list, "the candidate was never pushed"
        assert pushed.call_args_list[0].kwargs["sha"] == head
        assert any(
            call[:2] == ["workflow", "run"]
            and "ci-router.yml" in call
            and "run_all=true" in call
            for call in calls
        ), f"the release must dispatch the run_all workflow itself; got {calls}"

    def test_the_release_completes_at_the_same_version(self, tmp_project):
        core, bump_sha = self._staged(tmp_project)
        head = _git_head(tmp_project)
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        state_path = get_state_path(str(tmp_project), releasable_dir=rel_dir)
        calls = []

        with patch("rlsbl.commands.watch.run_gh",
                   side_effect=self._gh_recorder(head, calls)):
            _run_resume(tmp_project, core, remote_head=bump_sha)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "alpha@v1.0.1"
        assert not os.path.exists(state_path)

    def test_a_fresh_release_is_still_refused(self, tmp_project, capsys):
        """No prior published candidate: an empty window is a config defect."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        _setup_releasable_workspace(
            tmp_project, root_workflows=("ci-router.yml",),
        )
        head = _git_head(tmp_project)
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        state_path = get_state_path(str(tmp_project), releasable_dir=rel_dir)
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        save_release_state(state_path, {
            "release_commits": [],
            "completed_steps": ["VERSION_BUMPED", "COMMITTED"],
        })

        with pytest.raises(Exception) as exc:
            _guard_empty_candidate_window(
                candidate_sha=head, remote_head=head, needs_push=True,
                state_path=state_path,
                monorepo_root=str(tmp_project), monorepo_name="core",
                releasable_name="alpha",
                version="1.0.1", tag="alpha@v1.0.1", branch="main",
                cwd=str(tmp_project), log=lambda m: None,
            )
        assert "cannot trigger this project's CI" in str(exc.value)

    def test_no_router_on_disk_leaves_the_refusal_in_place(self, tmp_project):
        """Nothing to dispatch: the honest answer is still the hard error."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        core = _setup_releasable_workspace(tmp_project)
        state_path, bump_sha = _prepare_resumable_candidate(tmp_project, core)
        head = _git_head(tmp_project)

        with pytest.raises(Exception) as exc:
            _guard_empty_candidate_window(
                candidate_sha=head, remote_head=bump_sha, needs_push=True,
                state_path=state_path,
                monorepo_root=str(tmp_project), monorepo_name="core",
                releasable_name="alpha",
                version="1.0.1", tag="alpha@v1.0.1", branch="main",
                cwd=str(tmp_project), log=lambda m: None,
            )
        assert "cannot trigger this project's CI" in str(exc.value)


class TestCandidateAlreadyOnTheRemote:
    """A resume that owes no push must not be judged as if it owed one.

    When the remote branch is already AT the candidate, no push is about to
    happen: the CI run the gate will read is the one an earlier push triggered,
    and that push's own before-SHA is not knowable locally. The guard widens the
    window to the release's own commit trail for exactly this case
    (:func:`_widened_window_base`).

    Handing the guard a hardcoded ``needs_push=True`` instead makes it diff the
    candidate against itself -- an empty window every time -- so every such
    resume was refused with "nothing changed", naming a push that was not about
    to happen. That is the shape a ``run_all`` dispatch leaves behind: the
    candidate is on the remote, its jobs have all run, and the operator resumes
    without adding a commit.
    """

    def test_a_resume_with_no_push_owed_completes(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        state_path, _bump = _prepare_resumable_candidate(tmp_project, core)
        head = _git_head(tmp_project)

        # The remote is AT the local tip: nothing to push.
        _run_resume(tmp_project, core, remote_head=head)

        tags = subprocess.run(
            ["git", "tag", "--list", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tags == "alpha@v1.0.1", (
            "the widened window contains the version bump; the release must "
            "run to completion"
        )
        assert not os.path.exists(state_path)

    def test_the_guard_still_refuses_when_even_the_widened_window_is_empty(
        self, tmp_project, capsys
    ):
        """No relaxation: a trail that touches nothing of the project's is
        still refused, push owed or not."""
        from rlsbl.commands.release.execute import _guard_empty_candidate_window

        _setup_releasable_workspace(tmp_project)
        head = _git_head(tmp_project)
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        state_path = get_state_path(str(tmp_project), releasable_dir=rel_dir)
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        save_release_state(state_path, {"release_commits": []})

        with pytest.raises(Exception) as exc:
            _guard_empty_candidate_window(
                candidate_sha=head, remote_head=head, needs_push=False,
                state_path=state_path,
                monorepo_root=str(tmp_project), monorepo_name="core",
                releasable_name="alpha",
                version="1.0.1", tag="alpha@v1.0.1", branch="main",
                cwd=str(tmp_project), log=lambda m: None,
            )
        assert "cannot trigger this project's CI" in str(exc.value)
        assert "already published this commit" in str(exc.value)
