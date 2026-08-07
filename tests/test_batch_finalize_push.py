"""A batch release ends level with its remote, finalize commit included.

The batch flow is push-disciplined everywhere except its own last step: pass 1
releases every member with ``ci-defer`` (commit, never push), ONE candidate
push publishes the whole batch, the CI gate runs on that commit, and pass 2
tags and completes each member -- pushing as it goes. Then the archive gate
renames the batch file to ``batch-<ts>.toml`` and COMMITS it.

That commit used to be the end of the run. Nothing pushed it, so every
repository that ever completed a batch release stayed permanently one commit
ahead of its remote, carrying a ``chore: finalize batch release file`` commit
that never reached origin (observed twice in one week across two repos). The
commit is the release's own -- it archives the document describing the release
-- so the remote's history was missing an artifact the release produced, and
the next release's preflight met a diverged local branch.

The property under test: after a completed batch, HEAD is on the remote.
"""

import os
from unittest.mock import patch

from githarness import git

from rlsbl.commands.monorepo import batch_release

from test_batch_main_as_candidate import (  # noqa: E402
    _PushRecorder,
    _run_batch,
    _setup_batch_workspace,
    _setup_releasable_batch_workspace,
)


def _remote_tags_land():
    """Every tag the batch pushes is on the remote.

    The harness swallows real pushes, so the archive gate's remote-tag
    evidence (:func:`item_is_released`) would answer "not released" and the
    batch would never reach the finalize step this module is about.
    """
    return patch(
        "rlsbl.commands.monorepo.batch_plan.tag_exists_on_remote",
        return_value=True,
    )


def _run_batch_with_recorder(root, setup):
    """Run a whole batch against a recorder that models the remote branch."""
    setup(root)
    recorder = _PushRecorder(root, git(root, "rev-parse", "HEAD"))
    with _remote_tags_land():
        _run_batch(root, ci_return=("green", []), push_side_effect=recorder)
    return recorder


def _archived_batch_file(root):
    releases = root / ".rlsbl-monorepo" / "releases"
    return [
        f for f in os.listdir(releases)
        if f.startswith("batch-") and f.endswith(".toml")
    ]


class TestBatchEndsLevelWithItsRemote:

    def test_a_completed_package_batch_leaves_nothing_unpushed(self, tmp_project):
        recorder = _run_batch_with_recorder(tmp_project, _setup_batch_workspace)

        head = git(tmp_project, "rev-parse", "HEAD")
        assert recorder.remote == head, (
            "a completed batch must leave the branch level with its remote; "
            f"HEAD is {head[:12]} but the remote is at "
            f"{recorder.remote[:12]} -- the batch finalize commit was never "
            "pushed"
        )

    def test_a_completed_releasable_batch_leaves_nothing_unpushed(
        self, tmp_project,
    ):
        recorder = _run_batch_with_recorder(
            tmp_project, _setup_releasable_batch_workspace,
        )

        head = git(tmp_project, "rev-parse", "HEAD")
        assert recorder.remote == head, (
            "a completed releasable batch must leave the branch level with "
            f"its remote; HEAD is {head[:12]} but the remote is at "
            f"{recorder.remote[:12]}"
        )

    def test_the_archived_batch_file_reaches_the_remote(self, tmp_project):
        """Not just any commit: the archive itself must be published."""
        recorder = _run_batch_with_recorder(tmp_project, _setup_batch_workspace)

        archived = _archived_batch_file(tmp_project)
        assert len(archived) == 1, f"expected one archived batch file: {archived}"

        head = git(tmp_project, "rev-parse", "HEAD")
        window = recorder.window_ending_at(head)
        paths = recorder.paths_in(window)
        assert any(archived[0] in p for p in paths), (
            f"the final push must publish the archived {archived[0]}; it "
            f"published {paths}"
        )

    def test_the_finalize_push_is_the_batchs_own_last_step(self, tmp_project):
        """One extra push, publishing exactly the one finalize commit."""
        recorder = _run_batch_with_recorder(tmp_project, _setup_batch_workspace)

        head = git(tmp_project, "rev-parse", "HEAD")
        before, after = recorder.window_ending_at(head)
        assert after == head
        assert git(tmp_project, "rev-list", "--count", f"{before}..{after}") == "1", (
            "the finalize push must publish exactly the finalize commit"
        )
        assert git(tmp_project, "log", "-1", "--format=%s", head).startswith(
            "chore: finalize batch release file"
        )


class TestFinalizePushRefusesARideIn:
    """The finalize push is a release push: it never carries foreign work."""

    def _ride_in_at_finalize(self, root):
        """Land a foreign commit in the finalize window.

        The window is between the archive commit and its push, so the rider
        goes on AFTER the real finalize: that is the commit the push would
        otherwise sweep to the remote as part of the release.
        """
        real_finalize = batch_release._finalize_batch_file

        def _wrapped(batch_path, log):
            result = real_finalize(batch_path, log)
            (root / "rider.txt").write_text("rider\n")
            git(root, "add", "rider.txt")
            git(root, "commit", "-q", "-m", "concurrent session: unrelated work")
            return result

        return _wrapped

    def test_a_commit_that_is_not_the_batchs_own_is_never_pushed(
        self, tmp_project, capsys,
    ):
        _setup_batch_workspace(tmp_project)
        recorder = _PushRecorder(tmp_project, git(tmp_project, "rev-parse", "HEAD"))

        with _remote_tags_land(), \
                patch.object(batch_release, "_finalize_batch_file",
                             side_effect=self._ride_in_at_finalize(tmp_project)):
            _run_batch(
                tmp_project, ci_return=("green", []), push_side_effect=recorder,
            )

        head = git(tmp_project, "rev-parse", "HEAD")
        assert recorder.remote != head, (
            "a ride-in on the finalize window must NOT be published"
        )

        err = capsys.readouterr().err
        assert "concurrent session: unrelated work" in err, (
            "the refusal must name the foreign commit it declined to push"
        )
        assert "git push" in err, (
            "the operator must be told how to finish the push by hand"
        )
