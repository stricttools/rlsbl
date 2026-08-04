"""Range pin + refuse-on-drift (C5): foreign commits never ride into a release.

The release range is computed from the branch at run time. While a release is
in flight -- and the CI gate makes that window minutes long -- a concurrent
session sharing the worktree can land commits on the release branch, and they
used to join the release silently, shipping unreviewed under its changelog.

The release now pins HEAD at the top of its entry and records every commit it
creates in the state file's ``release_commits`` trail. Anything in
``pin..HEAD`` that is not in the trail is foreign, and the release hard-errors
naming it. This is the forward twin of the rollback clobber guard: that one
refuses to DESTROY a concurrent session's commits, this one refuses to SHIP
them.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.commands.release import run_cmd
from rlsbl.commands.release.execute import (
    ForeignCommitError,
    _guard_foreign_commits,
    head_sha,
)
from rlsbl.commands.release.release_state import (
    get_state_path,
    save_release_state,
)
from rlsbl.context import create_context
from rlsbl.workspace import get_releasable_dir

from test_representative_write_elimination import (  # noqa: E402
    _rc,
    _release_patches,
    _setup_releasable_workspace,
)


# --------------------------------------------------------------------------- #
# The guard in isolation
# --------------------------------------------------------------------------- #


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t.local")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit(repo, name, message):
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class TestGuardInIsolation:

    def test_an_empty_range_passes(self, tmp_path):
        repo = _repo(tmp_path)
        state = str(tmp_path / "state.json")
        save_release_state(state, {"release_commits": []})
        _guard_foreign_commits(
            head_sha(cwd=str(repo)), state, cwd=str(repo), phase="entry",
        )

    def test_the_releases_own_commits_never_trip_it(self, tmp_path):
        repo = _repo(tmp_path)
        pin = head_sha(cwd=str(repo))
        own = [
            _commit(repo, "selfdoc.txt", "selfdoc: regenerate"),
            _commit(repo, "bump.txt", "v1.0.1"),
            _commit(repo, "final.txt", "chore: finalize changelog for 1.0.1"),
        ]
        state = str(tmp_path / "state.json")
        save_release_state(state, {"release_commits": own})
        _guard_foreign_commits(pin, state, cwd=str(repo), phase="entry")

    def test_a_foreign_commit_aborts_and_is_named(self, tmp_path):
        repo = _repo(tmp_path)
        pin = head_sha(cwd=str(repo))
        mine = _commit(repo, "bump.txt", "v1.0.1")
        theirs = _commit(repo, "todo.md", "todo: file a note from another session")
        state = str(tmp_path / "state.json")
        save_release_state(state, {"release_commits": [mine]})

        with pytest.raises(ForeignCommitError) as exc:
            _guard_foreign_commits(pin, state, cwd=str(repo), phase="candidate push")

        msg = str(exc.value)
        assert theirs[:12] in msg, "the foreign SHA must be named"
        assert "todo: file a note from another session" in msg, (
            "the foreign commit's subject must be shown"
        )
        assert mine[:12] not in msg, "the release's own commit is not foreign"
        assert "candidate push" in msg, "the checkpoint must be named"
        assert "rlsbl changelog add" in msg, "including them must be an option"

    def test_an_unresolvable_pin_is_not_treated_as_drift(self, tmp_path):
        """A pin that git cannot resolve proves nothing either way; the
        rollback guard takes the same stance."""
        repo = _repo(tmp_path)
        state = str(tmp_path / "state.json")
        save_release_state(state, {"release_commits": []})
        _guard_foreign_commits("f" * 40, state, cwd=str(repo), phase="entry")
        _guard_foreign_commits(None, state, cwd=str(repo), phase="entry")


# --------------------------------------------------------------------------- #
# End to end: a ride-in during a real release
# --------------------------------------------------------------------------- #


def _state_path(root):
    return get_state_path("", releasable_dir=get_releasable_dir(str(root), "alpha"))


def _run(member_dir, root, extra=()):
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    patches = _release_patches(tuple(extra))
    for p in patches:
        p.start()
    try:
        run_cmd(_rc(), {"quiet": True, "skip-lock": True}, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


class TestRideInDuringRelease:

    def test_a_commit_landing_during_the_ci_gate_aborts_the_release(
        self, tmp_project, capsys,
    ):
        """The window that matters: a concurrent session commits while the
        release waits for CI on its candidate.

        Red-green: without the pin the ride-in is simply part of ``HEAD`` when
        the finalize/tag/push steps run, so it ships under this version.
        """
        core = _setup_releasable_workspace(tmp_project)

        def ci_gate_with_ride_in(sha, **kwargs):
            # Another session lands work on the release branch mid-wait.
            (tmp_project / "unrelated.md").write_text("someone else's work\n")
            git(tmp_project, "add", "unrelated.md")
            git(tmp_project, "commit", "-q", "-m", "docs: unrelated note")
            return "green", []

        with pytest.raises(SystemExit) as exc:
            _run(core, tmp_project, extra=(
                patch("rlsbl.commands.release.wait_for_ci_green",
                      side_effect=ci_gate_with_ride_in),
            ))
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "docs: unrelated note" in err, (
            "the ride-in must be named in the abort message"
        )

        # Nothing shipped: no tag, no finalized changelog.
        assert "alpha@v1.0.1" not in git(tmp_project, "tag", "-l").split()
        changes = os.path.join(
            get_releasable_dir(str(tmp_project), "alpha"), "changes",
        )
        assert not os.path.exists(os.path.join(changes, "1.0.1.jsonl"))

        # The concurrent session's commit is untouched -- the guard refuses to
        # ship it, never to destroy it.
        assert (tmp_project / "unrelated.md").exists()
        subjects = git(tmp_project, "log", "--format=%s", "-5").splitlines()
        assert "docs: unrelated note" in subjects

    def test_a_clean_release_never_trips_the_guard(self, tmp_project):
        """Every commit a release makes for itself -- selfdoc regeneration,
        the version bump, the snapshot, both finalize commits -- is in its own
        trail."""
        core = _setup_releasable_workspace(tmp_project)
        _run(core, tmp_project, extra=(
            patch("rlsbl.commands.release.wait_for_ci_green",
                  return_value=("green", [])),
        ))
        assert "alpha@v1.0.1" in git(tmp_project, "tag", "-l").split()

    def test_the_pin_is_recorded_in_the_release_state(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        pin_before = git(tmp_project, "rev-parse", "HEAD")
        captured = {}

        def capture_state(sha, **kwargs):
            with open(_state_path(tmp_project), encoding="utf-8") as f:
                captured.update(json.load(f))
            return "green", []

        _run(core, tmp_project, extra=(
            patch("rlsbl.commands.release.wait_for_ci_green",
                  side_effect=capture_state),
        ))

        assert captured.get("pin_sha") == pin_before, (
            "the pin must be HEAD as it was at the top of the release entry, "
            "before the pre-mutating selfdoc auto-commit"
        )
        # The trail covers every commit between the pin and the candidate.
        trail = set(captured.get("release_commits", []))
        candidate = captured.get("candidate_sha")
        assert candidate
        rev_list = subprocess.run(
            ["git", "rev-list", f"{pin_before}..{candidate}"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.split()
        assert set(rev_list) <= trail, (
            "every commit the release made must be in its own trail; "
            f"untracked: {sorted(set(rev_list) - trail)}"
        )
