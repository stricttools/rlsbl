"""`rlsbl release resume` integrity: dry runs, the verified candidate, ride-ins.

Three defects observed on one real release, all on the resume path:

1. ``rlsbl release resume --dry-run`` executed the ENTIRE release -- commits,
   tag, push to the release branch, GitHub Release, workflow dispatches.  The
   dry-run gate lived only in the fresh-release entry point; resume walked
   straight into the mutating phase.
2. Resume tagged whatever the branch tip happened to be and logged it as
   "(CI-verified)".  The mutating phase rewrote the state file from a fresh
   dict that had no ``candidate_sha`` key, so the recorded CI-verified
   candidate was erased on entry and the reader silently fell back to HEAD.
   The tagged commit had no CI runs at all and the publish gate refused it.
3. The refuse-on-drift guard could not fire on resume: resume re-pinned at the
   current tip unconditionally, so a concurrent session's commit was already
   inside the pin range and rode into the release.

The tests assert at the effect boundary (``rlsbl.effects`` is the single
chokepoint for process, filesystem and network mutations) and on the
repository's observable state.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl import effects
from rlsbl.commands.release import resume_cmd
from rlsbl.commands.release.execute import ForeignCommitError, UnverifiedCandidateError
from rlsbl.commands.release.release_state import (
    get_state_path,
    load_release_state,
    save_release_state,
)
from rlsbl.errors import RlsblError
from rlsbl.utils import run as real_run

from test_release_resume import (  # noqa: E402
    _git,
    _git_head,
    _git_output,
    _make_ctx,
    _setup_releasable_npm_project,
)


# --------------------------------------------------------------------------- #
# Effect-boundary recorder
# --------------------------------------------------------------------------- #

# Process invocations that mutate something outside this process.  Fail-closed
# on the two tools that reach the network or the object database.
_MUTATING_GIT_VERBS = {
    "commit", "push", "tag", "reset", "update-ref", "add", "rm", "mv",
    "checkout", "switch", "merge", "rebase", "cherry-pick", "revert",
    "apply", "am", "stash", "clean", "gc", "prune", "fetch", "pull",
    "init", "clone", "notes", "replace", "filter-branch",
}

# ``git tag``/``git branch``/``git remote`` also have pure listing forms.
_LIST_FLAGS = {"-l", "--list", "-n", "--contains", "--points-at", "-v"}

_FS_MUTATORS = (
    "open_write", "write_text", "append_text", "write_bytes",
    "atomic_write_text", "makedirs", "mkdir", "rename", "replace", "remove",
    "rmdir", "removedirs", "rmtree", "chmod", "copy_file", "copytree",
)


def _is_mutating_argv(argv):
    """True when *argv* would change something outside this process."""
    if not argv:
        return False
    exe = os.path.basename(str(argv[0]))
    if exe in ("gh", "safegit", "npm", "uv", "go", "curl"):
        return True
    if exe != "git" or len(argv) < 2:
        return False
    # Skip global options (-C <dir>, -c k=v) to find the subcommand.
    i = 1
    while i < len(argv) and str(argv[i]).startswith("-"):
        i += 2 if str(argv[i]) in ("-C", "-c") else 1
    if i >= len(argv):
        return False
    verb = str(argv[i])
    if verb not in _MUTATING_GIT_VERBS:
        return False
    if verb == "tag" and any(a in _LIST_FLAGS for a in argv[i + 1:]):
        return False
    return True


class EffectRecorder:
    """Wraps every ``rlsbl.effects`` mutator and records what was asked for."""

    def __init__(self, repo):
        self.repo = os.path.realpath(str(repo))
        self.processes = []
        self.filesystem = []
        self.network = []

    def _inside_repo(self, path):
        try:
            real = os.path.realpath(str(path))
        except (TypeError, ValueError):
            return False
        return real == self.repo or real.startswith(self.repo + os.sep)

    def install(self, monkeypatch):
        real_run_effect = effects.run
        real_gh = effects.gh
        real_urlopen = effects.urlopen

        def rec_run(argv, **kwargs):
            self.processes.append(list(argv))
            return real_run_effect(argv, **kwargs)

        def rec_gh(args, **kwargs):
            self.network.append(["gh", *args])
            return real_gh(args, **kwargs)

        def rec_urlopen(url, **kwargs):
            self.network.append(["urlopen", str(url)])
            return real_urlopen(url, **kwargs)

        monkeypatch.setattr(effects, "run", rec_run)
        monkeypatch.setattr(effects, "gh", rec_gh)
        monkeypatch.setattr(effects, "urlopen", rec_urlopen)

        for name in _FS_MUTATORS:
            real_fn = getattr(effects, name)

            def make(fn_name, fn):
                def wrapper(path, *args, **kwargs):
                    self.filesystem.append((fn_name, str(path)))
                    return fn(path, *args, **kwargs)
                return wrapper

            monkeypatch.setattr(effects, name, make(name, real_fn))

    # -- assertions ------------------------------------------------------- #

    def mutations(self):
        found = []
        found += [f"process: {' '.join(a)}" for a in self.processes
                  if _is_mutating_argv(a)]
        found += [f"network: {' '.join(n)}" for n in self.network]
        found += [f"fs: {op} {p}" for op, p in self.filesystem
                  if self._inside_repo(p)]
        return found


class RepoSnapshot:
    """Everything a dry run must leave byte-identical."""

    def __init__(self, repo, state_path):
        self.repo = repo
        self.state_path = state_path
        self.head = _git_head(repo)
        self.tags = sorted(_git_output(repo, "tag", "-l").split())
        self.status = _git_output(repo, "status", "--porcelain")
        self.state = (
            Path(state_path).read_bytes() if os.path.exists(state_path) else None
        )

    def assert_unchanged(self):
        assert _git_head(self.repo) == self.head, "a dry run created a commit"
        assert sorted(_git_output(self.repo, "tag", "-l").split()) == self.tags, \
            "a dry run created or moved a tag"
        assert _git_output(self.repo, "status", "--porcelain") == self.status, \
            "a dry run changed the working tree"
        now = (
            Path(self.state_path).read_bytes()
            if os.path.exists(self.state_path) else None
        )
        assert now == self.state, "a dry run rewrote the release state file"


# --------------------------------------------------------------------------- #
# Fixture: a release left in-flight just past the CI gate
# --------------------------------------------------------------------------- #


def _fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
    """Intercept gh and the remote-touching git verbs; run the rest for real."""
    if cmd == "gh":
        return ""
    if cmd == "git" and args and args[0] in ("push", "fetch"):
        return ""
    if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
            and any("origin/" in a for a in args)):
        return "0"
    if cmd == "git" and args and args[0] == "ls-remote":
        return ""
    return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)


def _resume_patches(ci_side_effect=None):
    ci_kwargs = (
        {"side_effect": ci_side_effect} if ci_side_effect is not None
        else {"return_value": ("green", [])}
    )
    return [
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=_fake_run),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
        patch("rlsbl.commands.release.wait_for_ci_green", **ci_kwargs),
    ]


def _resume(repo, state, flags, *, ci_side_effect=None):
    patches = _resume_patches(ci_side_effect=ci_side_effect)
    for p in patches:
        p.start()
    try:
        resume_cmd(state, flags, ctx=_make_ctx(repo))
    finally:
        for p in patches:
            p.stop()


def _resume_expect_abort(repo, state, flags, capsys):
    """Resume, require a clean non-zero exit, and return what the operator saw.

    ``resume_cmd`` converts every expected release failure into a one-line
    ``Error: ...`` on stderr plus ``sys.exit(1)``; the test asserts on that
    surface rather than on an exception that never reaches a user.
    """
    with pytest.raises(SystemExit) as excinfo:
        _resume(repo, state, flags)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    return captured.out + captured.err


def _in_flight_past_the_gate(repo, *, extra_release_commit=True):
    """Leave *repo* exactly as a run that failed after its CI gate went green.

    The version bump is committed (that commit is the candidate CI verified and
    the branch was pushed at it), the changelog was finalized on top of it, and
    the run then died before archiving the release file.  Returns
    ``(state_path, candidate_sha)``.
    """
    _setup_releasable_npm_project(repo)
    pin_sha = _git_head(repo)

    pkg = json.loads((repo / "package.json").read_text())
    pkg["version"] = "1.0.1"
    (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    _git(repo, "add", "package.json")
    _git(repo, "commit", "-q", "-m", "v1.0.1")
    candidate_sha = _git_head(repo)

    completed = [
        "VERSION_BUMPED", "COMMITTED", "SNAPSHOT_REGENERATED",
        "BRANCH_PUSHED", "CI_VERIFIED",
    ]
    trail = [candidate_sha]

    if extra_release_commit:
        # The original run got one step further before dying: it finalized the
        # changelog, which is a commit of its own on top of the candidate.
        changes = repo / ".rlsbl" / "changes"
        (changes / "1.0.1.jsonl").write_text(
            (changes / "unreleased.jsonl").read_text()
        )
        (changes / "unreleased.jsonl").write_text("")
        (changes / "1.0.1.md").write_text("## 1.0.1\n\n- **New feature.**\n")
        _git(repo, "add", ".rlsbl/changes")
        _git(repo, "commit", "-q", "-m", "chore: finalize changelog for 1.0.1")
        completed.append("CHANGELOG_FINALIZED")
        trail.append(_git_head(repo))

    state_path = get_state_path(str(repo))
    save_release_state(state_path, {
        "new_version": "1.0.1",
        "tag": "v1.0.1",
        "branch": "main",
        "pre_release_sha": pin_sha,
        "pin_sha": pin_sha,
        "candidate_sha": candidate_sha,
        "release_commits": trail,
        "bump_type": "patch",
        "registry": "npm",
        "completed_steps": completed,
        "failed_steps": {},
        "companion_tags": [],
        "monorepo_name": None,
        "releasable_name": None,
        "commit_msg": "v1.0.1",
        "description": "test release",
        "context": "",
        "include": ["npm"],
        "exclude": [],
        "preid": "",
        "blog": False,
    })
    return state_path, candidate_sha


# --------------------------------------------------------------------------- #
# Bug 1 -- `release resume --dry-run` must mutate nothing
# --------------------------------------------------------------------------- #


class TestResumeDryRun:

    def test_a_dry_run_resume_mutates_nothing(self, mock_git_repo, monkeypatch):
        """The whole point of --dry-run: preview, change nothing anywhere.

        Observed failure: this executed the release -- commit, tag, push to
        origin/main, GitHub Release, workflow dispatch.
        """
        state_path, _ = _in_flight_past_the_gate(mock_git_repo)
        snapshot = RepoSnapshot(mock_git_repo, state_path)

        recorder = EffectRecorder(mock_git_repo)
        recorder.install(monkeypatch)

        _resume(
            mock_git_repo,
            load_release_state(state_path),
            {"dry-run": True, "quiet": False},
        )

        mutations = recorder.mutations()
        assert mutations == [], (
            "`release resume --dry-run` must not mutate anything; it did:\n  "
            + "\n  ".join(mutations)
        )
        snapshot.assert_unchanged()

    def test_a_dry_run_resume_previews_the_remaining_work(
        self, mock_git_repo, monkeypatch, capsys,
    ):
        """A preview that prints nothing is indistinguishable from a no-op."""
        state_path, candidate_sha = _in_flight_past_the_gate(mock_git_repo)

        recorder = EffectRecorder(mock_git_repo)
        recorder.install(monkeypatch)
        _resume(
            mock_git_repo,
            load_release_state(state_path),
            {"dry-run": True, "quiet": False},
        )
        out = capsys.readouterr().out

        assert "1.0.1" in out
        assert "v1.0.1" in out
        assert candidate_sha[:12] in out, (
            "the preview must name the commit the tag would land on"
        )
        for step in ("RELEASE_FILE_FINALIZED", "TAGGED", "PUSHED",
                     "GITHUB_RELEASE"):
            assert step in out, f"the preview must list the remaining step {step}"

    def test_a_dry_run_release_does_not_delete_a_completed_state_file(
        self, mock_git_repo, capsys,
    ):
        """`release run` auto-clears a leftover complete state file.

        That is a real deletion, and it happened under --dry-run too: the
        auto-clear sat above the dry-run gate. A preview must report the
        leftover, not remove it.
        """
        from rlsbl.commands.release import run_cmd
        from rlsbl.commands.release.release_state import RELEASE_STEPS
        from rlsbl.release_file import ReleaseConfig

        _setup_releasable_npm_project(mock_git_repo)
        state_path = get_state_path(str(mock_git_repo))
        save_release_state(state_path, {
            "new_version": "1.0.1",
            "tag": "v1.0.1",
            "branch": "main",
            "registry": "npm",
            "completed_steps": list(RELEASE_STEPS),
            "failed_steps": {},
        })
        before = Path(state_path).read_bytes()

        patches = _resume_patches()
        patches.append(
            patch("rlsbl.commands.release.check_gh_installed", return_value=True)
        )
        patches.append(
            patch("rlsbl.commands.release.check_gh_auth", return_value=True)
        )
        for p in patches:
            p.start()
        try:
            run_cmd(
                ReleaseConfig(
                    bump="patch", include=["npm"], exclude=[],
                    description="test release",
                ),
                {"dry-run": True, "quiet": False,
                 "allow-dirty": True},
                ctx=_make_ctx(mock_git_repo),
            )
        finally:
            for p in patches:
                p.stop()

        assert os.path.exists(state_path), \
            "--dry-run must not delete the leftover release state file"
        assert Path(state_path).read_bytes() == before
        assert "would clear" in capsys.readouterr().out.lower()

    def test_the_mutating_phase_refuses_to_run_under_dry_run(self):
        """Structural backstop: no entry point can walk into it by omission."""
        from rlsbl.commands.release.execute import ReleaseState, _run_release_mutating

        state = ReleaseState(
            new_version="1.0.1", current_version="1.0.0", bump_type="patch",
            tag="v1.0.1", branch="main", resolved_targets=[],
            lock_dir=".rlsbl", flags={"dry-run": True}, log=lambda _m: None,
            ctx=None,
        )
        with pytest.raises(RlsblError, match="dry.run"):
            _run_release_mutating(state)


# --------------------------------------------------------------------------- #
# Bug 2 -- the tag lands on the recorded CI-verified candidate, or not at all
# --------------------------------------------------------------------------- #


class TestResumeTagsTheVerifiedCandidate:

    def test_the_tag_lands_on_the_recorded_candidate_not_the_branch_tip(
        self, mock_git_repo,
    ):
        """Observed failure: the tag landed on a commit with zero CI runs.

        The state recorded one candidate; the resume tagged the branch tip and
        logged it as "(CI-verified)".
        """
        state_path, candidate_sha = _in_flight_past_the_gate(mock_git_repo)
        assert _git_head(mock_git_repo) != candidate_sha, \
            "fixture precondition: the tip must have moved past the candidate"

        _resume(
            mock_git_repo,
            load_release_state(state_path),
            {"quiet": True},
        )

        tagged = _git_output(mock_git_repo, "rev-list", "-n", "1", "v1.0.1")
        assert tagged == candidate_sha, (
            "the tag must land on the CI-verified candidate recorded in the "
            f"state file ({candidate_sha[:12]}), not on {tagged[:12]}"
        )

    def test_a_missing_recorded_candidate_is_a_hard_error(
        self, mock_git_repo, capsys,
    ):
        """Never silently fall back to HEAD and call it CI-verified."""
        state_path, _ = _in_flight_past_the_gate(mock_git_repo)
        state = load_release_state(state_path)
        del state["candidate_sha"]
        save_release_state(state_path, state)

        output = _resume_expect_abort(
            mock_git_repo,
            load_release_state(state_path),
            {"quiet": True},
            capsys,
        )

        assert "CI_VERIFIED" in output and "candidate_sha" in output
        assert "v1.0.1" not in _git_output(mock_git_repo, "tag", "-l").split(), \
            "nothing may be tagged when the verified commit cannot be established"

    def test_an_unresolvable_recorded_candidate_is_a_hard_error(
        self, mock_git_repo, capsys,
    ):
        """A candidate the repository does not have proves nothing either."""
        state_path, _ = _in_flight_past_the_gate(mock_git_repo)
        state = load_release_state(state_path)
        state["candidate_sha"] = "0" * 40
        save_release_state(state_path, state)

        output = _resume_expect_abort(
            mock_git_repo,
            load_release_state(state_path),
            {"quiet": True},
            capsys,
        )
        assert "does not resolve" in output
        assert "v1.0.1" not in _git_output(mock_git_repo, "tag", "-l").split()

    def test_the_error_type_is_the_dedicated_unverified_candidate_error(
        self, mock_git_repo,
    ):
        """The failure has its own type so callers can distinguish it."""
        from rlsbl.commands.release import _resume_cmd_inner

        state_path, _ = _in_flight_past_the_gate(mock_git_repo)
        state = load_release_state(state_path)
        del state["candidate_sha"]
        save_release_state(state_path, state)

        patches = _resume_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(UnverifiedCandidateError):
                _resume_cmd_inner(
                    load_release_state(state_path),
                    {"quiet": True},
                    ctx=_make_ctx(mock_git_repo),
                )
        finally:
            for p in patches:
                p.stop()

    def test_the_mutating_phase_preserves_the_recorded_candidate(
        self, mock_git_repo,
    ):
        """The root cause: entering the mutating phase erased candidate_sha.

        The state file is rewritten on entry so a resume can record its own
        progress.  It used to be rebuilt from a fresh dict with no
        ``candidate_sha`` key, so every resume forgot which commit CI verified
        -- and each further resume forgot it again.
        """
        state_path, candidate_sha = _in_flight_past_the_gate(mock_git_repo)

        def failing_run_gh(args, **kwargs):
            if args and args[0] == "release":
                raise subprocess.CalledProcessError(1, "gh release")
            return ""

        patches = _resume_patches()
        patches = [p for p in patches
                   if getattr(p, "attribute", None) != "run_gh"]
        patches.append(
            patch("rlsbl.commands.release.run_gh", side_effect=failing_run_gh)
        )
        for p in patches:
            p.start()
        try:
            with pytest.raises(BaseException):
                resume_cmd(
                    load_release_state(state_path),
                    {"quiet": True},
                    ctx=_make_ctx(mock_git_repo),
                )
        finally:
            for p in patches:
                p.stop()

        surviving = load_release_state(state_path)
        assert surviving is not None, "a failed resume must preserve its state"
        assert surviving.get("candidate_sha") == candidate_sha, (
            "the recorded CI-verified candidate must survive the resume's own "
            "state rewrite so the next resume still tags the right commit"
        )


# --------------------------------------------------------------------------- #
# Bug 3 -- ride-ins abort on resume; deliberate fix-forwards are adopted
# --------------------------------------------------------------------------- #


class TestResumeRideIns:

    def test_a_foreign_commit_aborts_a_post_gate_resume(
        self, mock_git_repo, capsys,
    ):
        """Observed failure: a concurrent session's commit was pushed to main.

        Once CI has passed on the candidate the release is sealed to it, so a
        commit that is not part of the release's own trail is a ride-in.
        """
        state_path, candidate_sha = _in_flight_past_the_gate(mock_git_repo)

        (mock_git_repo / "other-session.txt").write_text("someone else's work\n")
        _git(mock_git_repo, "add", "other-session.txt")
        _git(mock_git_repo, "commit", "-q", "-m", "unrelated: another session")
        foreign_sha = _git_head(mock_git_repo)

        output = _resume_expect_abort(
            mock_git_repo,
            load_release_state(state_path),
            {"quiet": True},
            capsys,
        )

        assert foreign_sha[:12] in output, "the error must name the foreign SHA"
        assert "unrelated: another session" in output
        assert "v1.0.1" not in _git_output(mock_git_repo, "tag", "-l").split()
        assert _git_head(mock_git_repo) == foreign_sha, \
            "the foreign commit must be preserved, never rolled back"

    def test_the_ride_in_error_type_is_foreign_commit_error(self, mock_git_repo):
        from rlsbl.commands.release import _resume_cmd_inner

        state_path, _ = _in_flight_past_the_gate(mock_git_repo)
        (mock_git_repo / "other-session.txt").write_text("someone else's work\n")
        _git(mock_git_repo, "add", "other-session.txt")
        _git(mock_git_repo, "commit", "-q", "-m", "unrelated: another session")

        patches = _resume_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(ForeignCommitError):
                _resume_cmd_inner(
                    load_release_state(state_path),
                    {"quiet": True},
                    ctx=_make_ctx(mock_git_repo),
                )
        finally:
            for p in patches:
                p.stop()

    def test_a_fix_forward_resume_after_red_ci_adopts_the_fix(
        self, mock_git_repo,
    ):
        """The legitimate case the seal must not break.

        A red CI gate leaves the candidate unverified.  The documented remedy
        is to commit the fix on the release branch and resume: the new tip
        becomes the candidate, is pushed, and is gated again.
        """
        state_path, candidate_sha = _in_flight_past_the_gate(
            mock_git_repo, extra_release_commit=False,
        )
        state = load_release_state(state_path)
        state["completed_steps"] = [
            "VERSION_BUMPED", "COMMITTED", "SNAPSHOT_REGENERATED",
            "BRANCH_PUSHED",
        ]
        state["failed_steps"] = {"CI_VERIFIED": "Failing workflow(s): ci"}
        save_release_state(state_path, state)

        (mock_git_repo / "fix.txt").write_text("the fix\n")
        _git(mock_git_repo, "add", "fix.txt")
        _git(mock_git_repo, "commit", "-q", "-m", "fix: make CI green")
        fix_sha = _git_head(mock_git_repo)

        gated = []

        def fake_wait(sha, **kwargs):
            gated.append(sha)
            return "green", []

        _resume(
            mock_git_repo,
            load_release_state(state_path),
            {"quiet": True},
            ci_side_effect=fake_wait,
        )

        assert gated == [fix_sha], (
            "a fix-forward resume must re-gate the NEW tip, not the stale "
            f"candidate; gated {gated}"
        )
        assert _git_output(mock_git_repo, "rev-list", "-n", "1", "v1.0.1") == \
            fix_sha, "the tag must land on the newly verified fix commit"
