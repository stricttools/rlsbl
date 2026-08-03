"""Main-as-candidate release ordering (C4) and the failure modes it closes.

`rlsbl release run` pushes the version-bump commit to the release branch
UNTAGGED, waits for the repository's own CI to conclude on exactly that
commit, and only then finalizes the changelog, archives the release file,
tags the verified commit and creates the GitHub Release.

These tests are the regression suite for four items of the real-release
failure-mode inventory:

1.  All local mutations used to happen before CI had ever run, so a red CI
    left a pushed tag, a public GitHub Release and an immutable finalized
    changelog for a version that was never installable.
2.  Every failed attempt used to burn a version number; the fix now lands
    forward on the SAME version.
4.  The remediation guidance used to say "re-run CI to green on this exact
    commit", which is unfollowable when the failure is in the code at that
    commit. It now says: fix forward, same version, resume.
10. Orphan cleanup used to be manual, per-version and easy to skip. There is
    now nothing to clean up: a red candidate leaves no artifact behind.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.commands.release import resume_cmd, run_cmd
from rlsbl.commands.release.execute import _ci_red_message
from rlsbl.commands.release.release_state import (
    get_state_path,
    load_release_state,
)
from rlsbl.context import create_context
from rlsbl.workspace import get_releasable_changes_dir, get_releasable_dir

from test_representative_write_elimination import (  # noqa: E402
    _rc,
    _release_patches,
    _setup_releasable_workspace,
)


TAG = "alpha@v1.0.1"
VERSION = "1.0.1"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _ci_patch(verdict, results=()):
    """Patch the release flow's CI gate to a fixed verdict."""
    return patch(
        "rlsbl.commands.release.wait_for_ci_green",
        return_value=(verdict, list(results)),
    )


def _run(member_dir, root, *, verdict, gh_recorder=None):
    """Drive a real releasable release with a stubbed CI verdict."""
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    extra = [_ci_patch(verdict)]
    if gh_recorder is not None:
        extra.append(
            patch("rlsbl.commands.release.run_gh", side_effect=gh_recorder)
        )
    patches = _release_patches(tuple(extra))
    for p in patches:
        p.start()
    try:
        run_cmd(_rc(), {"yes": True, "quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _resume(member_dir, root, *, verdict, gh_recorder=None):
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    state = load_release_state(_state_path(root))
    assert state is not None, "a resumable release state must exist"
    extra = [_ci_patch(verdict)]
    if gh_recorder is not None:
        extra.append(
            patch("rlsbl.commands.release.run_gh", side_effect=gh_recorder)
        )
    patches = _release_patches(tuple(extra))
    for p in patches:
        p.start()
    try:
        resume_cmd(state, {"yes": True, "quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _state_path(root):
    return get_state_path("", releasable_dir=get_releasable_dir(str(root), "alpha"))


def _changes(root):
    return get_releasable_changes_dir(str(root), "alpha")


def _tags(root):
    return git(root, "tag", "-l").split()


def _gh_recorder(calls):
    def recorder(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["release", "view"]:
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    return recorder


def _add_fix_commit(root, core):
    """The operator's fix-forward commit, with its changelog entry."""
    (core / "fix.txt").write_text("the fix\n")
    git(root, "add", "packages/core/fix.txt")
    git(root, "commit", "-q", "-m", "fix the failing test")
    sha = git(root, "rev-parse", "HEAD")
    unreleased = os.path.join(_changes(root), "unreleased.jsonl")
    with open(unreleased, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "commits": [sha],
            "user_facing": True,
            "description": "**Fix the failure.** The bug is gone.",
            "type": "fix",
        }) + "\n")
    git(root, "add", os.path.relpath(unreleased, str(root)))
    git(root, "commit", "-q", "-m", "changelog: fix entry")
    return sha


# --------------------------------------------------------------------------- #
# Inventory modes 1 + 10: a red candidate leaves NO artifact behind
# --------------------------------------------------------------------------- #


class TestRedCILeavesNoArtifacts:

    def test_no_tag_no_release_no_finalization_while_ci_is_red(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        pre_sha = git(tmp_project, "rev-parse", "HEAD")
        gh_calls = []

        with pytest.raises(SystemExit) as exc:
            _run(core, tmp_project, verdict="red",
                 gh_recorder=_gh_recorder(gh_calls))
        assert exc.value.code == 1

        # (1) No tag, local or otherwise.
        assert TAG not in _tags(tmp_project), (
            "a red CI candidate must never be tagged"
        )

        # (1) No GitHub Release was created.
        assert not [c for c in gh_calls if c[:2] == ["release", "create"]], (
            "a red CI candidate must never produce a GitHub Release"
        )

        # (1) The changelog was NOT finalized: the entries are still
        # unreleased and no immutable per-version file exists.
        changes = _changes(tmp_project)
        assert not os.path.exists(os.path.join(changes, f"{VERSION}.jsonl")), (
            "no immutable version file may exist for an unreleased version"
        )
        assert os.path.getsize(os.path.join(changes, "unreleased.jsonl")) > 0, (
            "the entries must stay in unreleased.jsonl for the next attempt"
        )

        # (1) The release file was NOT archived.
        releases = os.path.join(get_releasable_dir(str(tmp_project), "alpha"),
                                "releases")
        assert not os.path.exists(os.path.join(releases, f"v{VERSION}.toml"))

        # (10) There is nothing to deprecate or yank: the only thing that
        # happened is that the candidate commit is on the branch.
        assert git(tmp_project, "rev-parse", "HEAD") != pre_sha, (
            "the candidate commit is published -- that is the design"
        )

        # The release is resumable at the same version.
        state = load_release_state(_state_path(tmp_project))
        assert state is not None
        assert state["new_version"] == VERSION
        assert "BRANCH_PUSHED" in state["completed_steps"]
        assert "CI_VERIFIED" not in state["completed_steps"]
        assert "CI_VERIFIED" in state.get("failed_steps", {})

    def test_a_red_gate_is_reported_with_fix_forward_guidance(
        self, tmp_project, capsys,
    ):
        """Inventory mode 4: the guidance must be followable.

        Red-green: the pre-C4 text told the operator to "re-run the CI
        workflow to green on this exact commit", which can never happen when
        the failure is in the code at that commit.
        """
        core = _setup_releasable_workspace(tmp_project)
        with pytest.raises(SystemExit):
            _run(core, tmp_project, verdict="red")

        err = capsys.readouterr().err
        assert "rlsbl release resume" in err
        assert "not burnt" in err.lower()
        assert "re-run CI on the same commit" in err
        assert "fix forward" in err.lower()


# --------------------------------------------------------------------------- #
# Inventory mode 2: the same version releases after a fix commit
# --------------------------------------------------------------------------- #


class TestFixForwardKeepsTheVersion:

    def test_same_version_releases_after_a_fix_commit(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)

        with pytest.raises(SystemExit):
            _run(core, tmp_project, verdict="red")

        fix_sha = _add_fix_commit(tmp_project, core)
        _resume(core, tmp_project, verdict="green")

        # The SAME version is released -- no number was burnt.
        assert TAG in _tags(tmp_project)
        assert "alpha@v1.0.2" not in _tags(tmp_project), (
            "a red attempt must not consume a version number"
        )

        # The tag points at the re-pushed candidate, which contains the fix.
        tag_sha = git(tmp_project, "rev-list", "-n", "1", TAG)
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", fix_sha, tag_sha],
            cwd=str(tmp_project),
        ).returncode == 0, "the tagged commit must contain the fix"

        # The changelog is finalized exactly once, for this version, and it
        # carries the fix entry recorded during the fix-forward.
        changes = _changes(tmp_project)
        with open(os.path.join(changes, f"{VERSION}.jsonl"), encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert any(fix_sha in e["commits"] for e in entries), (
            "the fix commit's changelog entry must be sealed into the release"
        )
        changelog = os.path.join(
            get_releasable_dir(str(tmp_project), "alpha"), "CHANGELOG.md",
        )
        with open(changelog, encoding="utf-8") as f:
            body = f.read()
        assert "Fix the failure" in body, (
            "the fix-forward entry must reach the changelog readers see"
        )

        # State cleared: the release completed.
        assert load_release_state(_state_path(tmp_project)) is None


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #


class TestCandidateIsPushedBeforeTheGate:

    def test_the_gate_runs_on_the_pushed_candidate_and_tags_it(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        seen = {}

        def fake_wait(commit_sha, **kwargs):
            seen["sha"] = commit_sha
            return "green", []

        ctx = create_context(Path(str(core)), workspace_root=Path(str(tmp_project)))
        patches = _release_patches((
            patch("rlsbl.commands.release.wait_for_ci_green", side_effect=fake_wait),
        ))
        for p in patches:
            p.start()
        try:
            run_cmd(_rc(), {"yes": True, "quiet": True, "skip-lock": True}, ctx=ctx)
        finally:
            for p in patches:
                p.stop()

        assert "sha" in seen, "the CI gate must run"
        assert git(tmp_project, "rev-list", "-n", "1", TAG) == seen["sha"], (
            "the tag must be created on exactly the commit CI verified"
        )


class TestNoCIConfigured:

    def test_a_repo_without_push_triggered_workflows_does_not_hang(self, tmp_project):
        """The fixture workspace has no .github/workflows: the real gate must
        return `no-ci` immediately rather than blocking on runs that can never
        appear."""
        from rlsbl.commands.watch import (
            CI_NOT_CONFIGURED, push_triggered_workflows, wait_for_ci_green,
        )

        assert push_triggered_workflows(str(tmp_project)) == []
        verdict, results = wait_for_ci_green(
            "0" * 40, timeout=1, repo_root=str(tmp_project), log=lambda _m: None,
        )
        assert verdict == CI_NOT_CONFIGURED
        assert results == []

    def test_only_a_push_trigger_counts_as_configured_ci(self, tmp_path):
        from rlsbl.commands.watch import push_triggered_workflows

        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "publish.yml").write_text("name: publish\non: workflow_dispatch\n")
        (wf / "notes.txt").write_text("on: push\n")
        assert push_triggered_workflows(str(tmp_path)) == []

        (wf / "ci.yml").write_text("name: ci\non:\n  push:\n    branches: [main]\n")
        assert push_triggered_workflows(str(tmp_path)) == ["ci.yml"]

    def test_configured_ci_that_produces_no_runs_is_a_hard_error(self, tmp_path):
        """Bounded grace, then a hard error -- never an unbounded wait and
        never a silent proceed."""
        from rlsbl.commands.watch import CIWaitError, wait_for_ci_green

        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: ci\non: push\n")

        with (
            patch("rlsbl.commands.watch.poll_runs", return_value=[]),
            patch("rlsbl.commands.watch.run_gh", return_value='{"nameWithOwner": "o/r"}'),
        ):
            with pytest.raises(CIWaitError) as exc:
                wait_for_ci_green(
                    "0" * 40, timeout=5, repo_root=str(tmp_path),
                    discovery_grace=0, log=lambda _m: None,
                )
        assert "ci.yml" in str(exc.value)
        assert "rlsbl release resume" in str(exc.value)


class TestRedMessageContent:
    """Inventory mode 4, at the message level."""

    def test_message_never_suggests_re_running_ci_on_the_same_commit(self):
        msg = _ci_red_message(
            version="1.2.3", tag="v1.2.3", branch="main",
            candidate_sha="a" * 40, detail="Failing workflow(s): test",
        )
        assert "rlsbl release resume" in msg
        assert "1.2.3" in msg
        # The pre-C4 (unfollowable) instruction must be gone.
        assert "re-run the CI workflow to green on this exact commit" not in msg
        assert "fails identically every time" in msg
        # Mode 10: it must say there is nothing to clean up.
        assert "no orphan version file to clean up" in msg
        assert "no GitHub Release exists" in msg

    def test_publish_gate_remediation_is_fix_forward_not_retry(self):
        from rlsbl.publish_gate import GATE_POLL_SCRIPT

        assert "re-run the CI workflow to green on this exact commit" not in \
            GATE_POLL_SCRIPT
        assert "rlsbl release deprecate" in GATE_POLL_SCRIPT
        assert "Do NOT re-dispatch this publish workflow" in GATE_POLL_SCRIPT


# --------------------------------------------------------------------------- #
# A CI TIMEOUT is not a red CI
# --------------------------------------------------------------------------- #


class TestCITimeoutIsNotRed:
    """A wait that ran out of time proves nothing about the candidate.

    Regression: _watch_single_run returned ``passed=False`` on a
    TimeoutExpired, so an unresolved run was aggregated into a red verdict and
    the operator was handed the deterministic-failure remedy ("a failure baked
    into the code fails identically every time") for runs that were very
    possibly still going.
    """

    def test_timeout_gets_its_own_honest_message(self, tmp_project, capsys):
        core = _setup_releasable_workspace(tmp_project)

        with pytest.raises(SystemExit) as exc:
            _run(core, tmp_project, verdict="timeout")
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "ran out of time" in err
        assert "may still be in progress" in err
        assert "NOT a CI failure" in err
        assert "rlsbl watch" in err, "the operator must be told how to check"
        assert "rlsbl release resume" in err
        assert "fails identically every time" not in err, (
            "the deterministic-failure remedy must not be given for a timeout"
        )

    def test_timeout_leaves_the_release_resumable_at_the_same_version(
        self, tmp_project,
    ):
        core = _setup_releasable_workspace(tmp_project)
        with pytest.raises(SystemExit):
            _run(core, tmp_project, verdict="timeout")

        assert TAG not in _tags(tmp_project)
        state = load_release_state(_state_path(tmp_project))
        assert state is not None and state["new_version"] == VERSION
        assert "CI_VERIFIED" in state.get("failed_steps", {})
        assert "Unresolved workflow(s)" in state["failed_steps"]["CI_VERIFIED"]

    def test_timeout_message_content(self):
        from rlsbl.commands.release.execute import _ci_timeout_message

        msg = _ci_timeout_message(
            version="1.2.3", tag="v1.2.3", branch="main",
            candidate_sha="a" * 40,
            detail="Unresolved workflow(s) after 60s: ci",
        )
        assert "NOT a CI failure" in msg
        assert "not burnt" in msg.lower()
        assert "--ci-timeout" in msg, "the budget knob must be named"
        assert "fails identically every time" not in msg


class TestNoCIGateNoticeSurvivesQuiet:
    """The 'proceeding without a CI gate' notice is the only signal that a
    release shipped ungated -- --quiet must not be able to swallow it."""

    def test_notice_reaches_stderr_under_quiet(self, tmp_project, capsys):
        core = _setup_releasable_workspace(tmp_project)
        _run(core, tmp_project, verdict="no-ci")
        err = capsys.readouterr().err
        assert "without a CI gate" in err
