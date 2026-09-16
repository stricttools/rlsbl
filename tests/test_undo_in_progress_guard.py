"""`release undo` refuses while a release the record does not contain is in flight.

Undo picks the version it reverts from the committed release archives alone.
A release that stopped before its archive step (``RELEASE_FILE_FINALIZED``,
the seventh of sixteen) is therefore invisible to it, and the version it picks
is the PREVIOUS, published one.

That was observed live: a repository held ``.rlsbl/releases/in-progress.json``
naming 0.29.3 with five steps completed, no v0.29.3 tag and no archive for it.
An operator discarding that half-finished release with `rlsbl release undo`
would have deleted the tag and the GitHub Release of 0.29.2 instead -- without
the plan ever naming the version they had in mind. Two of rlsbl's own messages
sent operators there.

So undo now refuses that state, and the two messages stop routing to it.
A leftover state file naming a version that IS recorded still undoes: that
release reached its archive step, and undo's own selection is the version the
operator means.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import git, remote_ref
from rlsbl.commands.release.release_state import (
    RELEASE_STEPS,
    get_state_path,
    load_release_state,
    save_release_state,
)

from test_remedy_followability import assert_invocation_is_real, invocations_in
from test_undo import _FakeGh, _make_real_shape_repo, _run_undo


STALLED_STEPS = [
    "VERSION_BUMPED", "COMMITTED", "SNAPSHOT_REGENERATED",
    "BRANCH_PUSHED", "CI_VERIFIED",
]


def _stall(repo, version, *, completed=None, candidate_sha=None):
    """Write the state file a release stopped mid-flight leaves behind."""
    state = {
        "new_version": version,
        "tag": f"v{version}",
        "branch": "main",
        "pre_release_sha": git(repo, "rev-parse", "HEAD"),
        "bump_type": "patch",
        "registry": "npm",
        "completed_steps": list(STALLED_STEPS if completed is None else completed),
        "failed_steps": {},
        "companion_tags": [],
        "monorepo_name": None,
        "releasable_name": None,
        "commit_msg": f"v{version}",
        "description": "test release",
        "context": "",
        "include": ["npm"],
        "exclude": [],
        "preid": "",
        "blog": False,
    }
    if candidate_sha:
        state["candidate_sha"] = candidate_sha
    path = get_state_path(str(repo))
    save_release_state(path, state)
    return path


def _released_repo(tmp_path, monkeypatch):
    """A repository whose 1.0.1 release completed and is recorded."""
    repo = tmp_path / "repo"
    _make_real_shape_repo(repo)
    monkeypatch.chdir(repo)
    return repo


def _refusal(repo, flags=None):
    """Run undo, require a refusal, and return what it printed on stderr."""
    with pytest.raises(SystemExit) as exc:
        _run_undo(repo, flags or {})
    assert exc.value.code == 1
    return exc.value


# --------------------------------------------------------------------------- #
# The refusal
# --------------------------------------------------------------------------- #

class TestAnUnrecordedInProgressReleaseIsRefused:

    def test_the_refusal_names_the_version_in_flight(
        self, tmp_path, monkeypatch, capsys,
    ):
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        _refusal(repo)

        err = capsys.readouterr().err
        assert "1.0.2" in err, err
        assert "in progress" in err, err

    def test_the_refusal_names_the_release_it_would_have_reverted(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The whole point: the version undo WOULD have picked is the wrong one."""
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        _refusal(repo)

        assert "1.0.1" in capsys.readouterr().err

    def test_the_refusal_distinguishes_unrecorded_from_recorded(
        self, tmp_path, monkeypatch, capsys,
    ):
        """It must say WHY this version is not undoable, not merely that it isn't."""
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        _refusal(repo)

        err = capsys.readouterr().err
        assert "record" in err, err
        assert "v1.0.2.toml" in err, err

    def test_the_previous_release_is_untouched(self, tmp_path, monkeypatch):
        """Nothing of 1.0.1's -- tag, remote tag, archive, version -- may move."""
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        head_before = git(repo, "rev-parse", "HEAD")
        gh = _FakeGh()

        with pytest.raises(SystemExit):
            _run_undo(repo, {}, gh=gh)

        assert "v1.0.1" in git(repo, "tag", "-l").split()
        assert remote_ref(repo, "refs/tags/v1.0.1") != ""
        assert (repo / ".rlsbl/releases/v1.0.1.toml").exists()
        assert json.loads((repo / "package.json").read_text())["version"] == "1.0.1"
        assert git(repo, "rev-parse", "HEAD") == head_before
        assert not gh.deleted, "no GitHub Release may be deleted"
        assert not (repo / ".rlsbl" / "undo-audit.json").exists()

    def test_dry_run_is_refused_too(self, tmp_path, monkeypatch, capsys):
        """A preview of a forbidden operation is not a preview worth printing."""
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        _refusal(repo, {"dry-run": True})

        out, err = capsys.readouterr()
        assert "1.0.2" in err
        assert "Undo plan" not in out, out

    def test_the_explicit_version_path_is_refused_too(
        self, tmp_path, monkeypatch, capsys,
    ):
        """`--version` names a target, but the repository is still mid-release.

        The refusal covers the whole command, not only the implicit
        latest-release selection: a repository holding an unrecorded release in
        flight is not a repository to be reverting versions in.
        """
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        _refusal(repo, {"version": "1.0.1"})

        assert "1.0.2" in capsys.readouterr().err

    def test_a_state_file_naming_no_version_is_refused(
        self, tmp_path, monkeypatch, capsys,
    ):
        """A record that does not say which release is in flight answers nothing."""
        repo = _released_repo(tmp_path, monkeypatch)
        path = get_state_path(str(repo))
        save_release_state(path, {"branch": "main", "completed_steps": []})

        _refusal(repo)

        assert "names no version" in capsys.readouterr().err

    def test_an_unreadable_state_file_is_refused(
        self, tmp_path, monkeypatch, capsys,
    ):
        """An unreadable record is never read as "no release in flight"."""
        repo = _released_repo(tmp_path, monkeypatch)
        path = Path(get_state_path(str(repo)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json\n")

        _refusal(repo)

        err = capsys.readouterr().err
        assert "could not be read" in err
        assert "in-progress.json" in err


# --------------------------------------------------------------------------- #
# The other case: the version in flight IS recorded
# --------------------------------------------------------------------------- #

class TestARecordedInProgressReleaseStillUndoes:
    """A leftover state file for a version the record contains changes nothing.

    That release reached its archive step, so undo's own selection and the
    version in flight are the same version: the behaviour that was already
    correct must not move.
    """

    def test_undo_reverts_the_recorded_version(self, tmp_path, monkeypatch):
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.1", completed=STALLED_STEPS + [
            "CHANGELOG_FINALIZED", "RELEASE_FILE_FINALIZED", "TAGGED",
        ])

        gh = _run_undo(repo, {})

        assert json.loads((repo / "package.json").read_text())["version"] == "1.0.0"
        assert "v1.0.1" not in git(repo, "tag", "-l").split()
        assert gh.deleted

    def test_no_in_progress_record_at_all_still_undoes(self, tmp_path, monkeypatch):
        repo = _released_repo(tmp_path, monkeypatch)

        gh = _run_undo(repo, {})

        assert json.loads((repo / "package.json").read_text())["version"] == "1.0.0"
        assert gh.deleted


# --------------------------------------------------------------------------- #
# The remedies the refusal prints, executed
# --------------------------------------------------------------------------- #

class TestTheRefusalsRemedies:

    def _text(self, repo, capsys):
        _refusal(repo)
        return capsys.readouterr().err

    def test_every_invocation_it_prints_is_a_real_command(
        self, tmp_path, monkeypatch, capsys,
    ):
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        text = self._text(repo, capsys)

        invocations = invocations_in(text)
        assert invocations, f"the refusal names no remedy at all: {text}"
        for invocation in invocations:
            assert_invocation_is_real(invocation)

    def test_it_names_no_discard_command(self, tmp_path, monkeypatch, capsys):
        """There is no command that discards a half-finished release.

        The refusal therefore describes the manual route and says so, rather
        than inventing a `release abort`/`release discard` for an operator to
        type and be told does not exist.
        """
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        text = self._text(repo, capsys)

        for invented in ("release abort", "release discard", "release cancel"):
            assert invented not in text, text

    def test_following_the_abandon_remedy_clears_the_refusal(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The refusal names a file to delete; deleting it lets undo through."""
        repo = _released_repo(tmp_path, monkeypatch)
        state_path = _stall(repo, "1.0.2")

        text = self._text(repo, capsys)
        assert state_path in text, text

        os.remove(state_path)

        _run_undo(repo, {"dry-run": True})
        assert "v1.0.1" in capsys.readouterr().out

    def test_following_the_finish_remedy_clears_the_refusal(
        self, tmp_path, monkeypatch, capsys,
    ):
        """`rlsbl release resume` is run for real, and the refusal is gone.

        The resumed release reaches its archive step, which is the whole
        difference between the two cases: once 1.0.2 is recorded, undo reverts
        1.0.2 rather than refusing.
        """
        from rlsbl.commands.release import resume_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.utils import run as real_run

        repo = _released_repo(tmp_path, monkeypatch)

        # The candidate commit of a 1.0.2 release that got as far as CI.
        pkg = json.loads((repo / "package.json").read_text())
        pkg["version"] = "1.0.2"
        (repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        (repo / ".rlsbl/changes/unreleased.jsonl").write_text(json.dumps({
            "commits": [], "user_facing": True,
            "description": "**New feature.** Another thing.", "type": "feature",
        }) + "\n")
        (repo / ".rlsbl/releases/unreleased.toml").write_text(
            'format_version = 1\nbump = "patch"\ninclude = ["npm"]\n'
            'exclude = []\ndescription = "another release"\n'
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "v1.0.2")
        candidate = git(repo, "rev-parse", "HEAD")
        state_path = _stall(repo, "1.0.2", candidate_sha=candidate)

        text = self._text(repo, capsys)
        assert "rlsbl release resume" in text, text

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            if cmd == "gh":
                return ""
            if cmd == "git" and args and args[0] in ("push", "fetch"):
                return ""
            if (cmd == "git" and args and args[:2] == ["rev-list", "--count"]
                    and any("origin/" in a for a in args)):
                return "0"
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        with (
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run_gh", return_value=""),
            patch("rlsbl.commands.release.run", side_effect=fake_run),
        ):
            resume_cmd(
                load_release_state(state_path),
                {"quiet": True},
                ctx=ProjectContext(
                    project_root=Path(str(repo)), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )

        assert (repo / ".rlsbl/releases/v1.0.2.toml").exists(), \
            "the remedy must reach the step that records the release"

        capsys.readouterr()
        _run_undo(repo, {"dry-run": True})
        out = capsys.readouterr().out
        assert "v1.0.2" in out, out


# --------------------------------------------------------------------------- #
# The two messages that routed operators into the wrong undo
# --------------------------------------------------------------------------- #

class TestTheMessagesThatRoutedHere:
    """Neither message may offer `release undo` for a version undo refuses."""

    def _guard_message(self, repo, capsys):
        """The `release run` in-progress refusal, as its own message."""
        from rlsbl.commands.release.validate import ReleaseValidationError
        from rlsbl.commands.release import run_cmd as release_run_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.release_file import ReleaseConfig

        ctx = ProjectContext(
            project_root=Path(str(repo)), workspace_root=None,
            config={"publish_mode": "ci", "pipelines": {}},
        )
        with pytest.raises(SystemExit):
            release_run_cmd(
                ReleaseConfig(
                    bump="patch", include=["npm"], exclude=[],
                    description="another release",
                ),
                {"allow-dirty": False, "watch": False, "dry-run": True},
                ctx=ctx,
            )
        return capsys.readouterr().err

    def test_release_run_does_not_offer_undo_for_an_unrecorded_version(
        self, tmp_path, monkeypatch, capsys,
    ):
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.2")

        err = self._guard_message(repo, capsys)

        assert "a previous release is in progress" in err, err
        assert "rlsbl release resume" in err, err
        assert "does not apply" in err, err

    def test_release_run_still_offers_undo_for_a_recorded_version(
        self, tmp_path, monkeypatch, capsys,
    ):
        repo = _released_repo(tmp_path, monkeypatch)
        _stall(repo, "1.0.1")

        err = self._guard_message(repo, capsys)

        assert "rlsbl release undo` to roll back" in err, err

    def test_the_unverified_candidate_remedy_drops_undo_when_unrecorded(
        self, tmp_path, monkeypatch,
    ):
        """`require_recorded_candidate` used to say "roll back with undo".

        It fires mid-release, where the version usually has no archive yet --
        so the rollback it named is the one undo now refuses.
        """
        from rlsbl.commands.release.execute import (
            UnverifiedCandidateError,
            require_recorded_candidate,
        )

        repo = _released_repo(tmp_path, monkeypatch)
        state_path = _stall(repo, "1.0.2")

        with pytest.raises(UnverifiedCandidateError) as exc:
            require_recorded_candidate(state_path, cwd=str(repo), version="1.0.2")

        message = str(exc.value)
        assert "rlsbl release undo` and" not in message, message
        assert "does not apply" in message, message
        for invocation in invocations_in(message):
            assert_invocation_is_real(invocation)

    def test_the_unverified_candidate_remedy_keeps_undo_when_recorded(
        self, tmp_path, monkeypatch,
    ):
        from rlsbl.commands.release.execute import (
            UnverifiedCandidateError,
            require_recorded_candidate,
        )

        repo = _released_repo(tmp_path, monkeypatch)
        state_path = _stall(repo, "1.0.1")

        with pytest.raises(UnverifiedCandidateError) as exc:
            require_recorded_candidate(state_path, cwd=str(repo), version="1.0.1")

        assert "roll back with `rlsbl release undo`" in str(exc.value)


def test_the_archive_step_is_the_seventh_of_sixteen():
    """The refusal's premise: a release records itself well after it starts.

    Five completed steps is short of the archive, which is why the live case
    had no v0.29.3.toml to be found. If the step order ever changes so that
    the archive is written first, this whole guard is describing a state that
    no longer occurs and must be revisited rather than left standing.
    """
    assert RELEASE_STEPS.index("RELEASE_FILE_FINALIZED") > len(STALLED_STEPS) - 1
