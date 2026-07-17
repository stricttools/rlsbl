"""Tests for the pre-tag snapshot reorder (1.2 snapshot before tag).

Covers:

(a) A monorepo release lands the snapshot commit BEFORE the tag, so the tag
    points at the branch tip that gets pushed and the snapshot commit is an
    ancestor of (here, equal to) the tag.
(b) `release undo` unwinds a release containing the snapshot commit
    completely (the walker recognizes the "snapshot" commit subject).
(c) The snapshot is regenerated pre-tag even in batch mode (the batch
    orchestrator loops run_cmd per member with batch-mode set; batch-mode
    must not move the snapshot back after the push).
(d) An old-ordering state file (completed through PUSHED with the snapshot
    slot never recorded) resumes cleanly: the forfeited pre-tag slot is
    regenerated post-hoc rather than failing.

The CI-SHA release-notes marker (1.3) is covered in test_ci_sha_marker.py.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from githarness import add_remote, git

from rlsbl.commands.release import run_cmd
from rlsbl.commands.release.release_state import RELEASE_STEPS
from rlsbl.context import create_context

# Proven real-release / real-undo harness helpers.
from test_representative_write_elimination import (  # noqa: E402
    _rc,
    _release_patches,
    _run_release,
    _setup_releasable_workspace,
)
from test_undo import _make_released_repo, _run_undo  # noqa: E402


_SNAPSHOT_REL = os.path.join(".rlsbl-monorepo", "snapshot.json")


def _run_release_flags(member_dir, root, flags, extra_patches=()):
    """Drive a real releasable release with explicit flags (mirrors
    test_representative_write_elimination._run_release but flag-configurable)."""
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    patches = _release_patches(extra_patches)
    for p in patches:
        p.start()
    try:
        run_cmd(_rc(), flags, ctx=ctx)
    finally:
        for p in patches:
            p.stop()


def _rev(root, ref):
    return git(root, "rev-list", "-n", "1", ref)


# --------------------------------------------------------------------------- #
# (a) Snapshot commit lands before the tag; tag is the push tip
# --------------------------------------------------------------------------- #

class TestSnapshotBeforeTag:

    def test_tag_is_push_tip_and_snapshot_is_ancestor(self, tmp_project):
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        tag_sha = _rev(tmp_project, "alpha@v1.0.1")
        head_sha = git(tmp_project, "rev-parse", "HEAD")

        # The tag points at the branch tip that gets pushed.
        assert tag_sha == head_sha, "the tag must be the pushed branch tip"

        # The tag commit IS the snapshot commit (snapshot lands last, pre-tag).
        tag_subject = git(tmp_project, "log", "-1", "--format=%s", tag_sha)
        assert tag_subject == "snapshot", (
            "the snapshot commit must be the tag tip (regenerated pre-tag)"
        )

        # Explicitly: the snapshot commit is an ancestor-or-equal of the tag.
        snap_sha = git(
            tmp_project, "log", "-1", "--format=%H", "--grep", "^snapshot$",
            "alpha@v1.0.1",
        )
        assert snap_sha, "a snapshot commit must exist in the release history"
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", snap_sha, tag_sha],
            cwd=str(tmp_project),
        )
        assert anc.returncode == 0, "snapshot commit must be an ancestor of the tag"

        # The snapshot file is committed (part of the tag commit's tree).
        assert (tmp_project / ".rlsbl-monorepo" / "snapshot.json").exists()


# --------------------------------------------------------------------------- #
# (c) Batch mode does not move the snapshot back after the push
# --------------------------------------------------------------------------- #

class TestBatchModeSnapshotPreTag:

    def test_batch_mode_member_snapshot_is_pre_tag(self, tmp_project):
        """The batch orchestrator releases each member via run_cmd with
        batch-mode set. Batch-mode only governs the final watch step; the
        per-member snapshot must still land pre-tag (tag == snapshot tip)."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release_flags(
            core, tmp_project,
            {"yes": True, "quiet": True, "skip-lock": True, "batch-mode": True},
        )

        tag_sha = _rev(tmp_project, "alpha@v1.0.1")
        head_sha = git(tmp_project, "rev-parse", "HEAD")
        assert tag_sha == head_sha
        assert git(tmp_project, "log", "-1", "--format=%s", tag_sha) == "snapshot", (
            "batch-mode must not push the snapshot past the tag"
        )


# --------------------------------------------------------------------------- #
# (b) Undo unwinds a release containing the snapshot commit
# --------------------------------------------------------------------------- #

class TestUndoUnwindsSnapshotCommit:

    def test_undo_reverts_snapshot_commit(self, tmp_path, monkeypatch):
        """A release whose newest commit is the pre-tag snapshot commit must
        undo completely: the walker recognizes the 'snapshot' subject and
        reverts it along with the rest of the release.

        Red-green: without the walker recognizing 'snapshot', the walk stops
        at the snapshot commit (treated as the pre-release boundary), reverts
        nothing, and package.json stays at 1.0.1."""
        repo = tmp_path / "repo"
        _make_released_repo(repo, n_commits=5, with_remote=False)

        # Reproduce the new ordering: the snapshot commit sits between the
        # finalize commits and the tag. Move the tag onto it.
        git(repo, "tag", "-d", "v1.0.1")
        (repo / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (repo / ".rlsbl-monorepo" / "snapshot.json").write_text('{"packages": {}}\n')
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "snapshot")
        git(repo, "tag", "v1.0.1")
        add_remote(repo, repo.parent / "remote.git")
        monkeypatch.chdir(repo)

        _run_undo(repo, {"yes": True})

        pkg = json.loads((repo / "package.json").read_text())
        assert pkg["version"] == "1.0.0", (
            "all release commits including the snapshot commit must be reverted"
        )
        assert not (repo / ".rlsbl-monorepo" / "snapshot.json").exists(), (
            "reverting the snapshot commit must remove snapshot.json"
        )
        assert "v1.0.1" not in git(repo, "tag", "-l").split()


# --------------------------------------------------------------------------- #
# (d) Old-ordering state-file resume: forfeited pre-tag slot regenerates post-hoc
# --------------------------------------------------------------------------- #

class TestOldOrderingResume:

    def test_resume_forfeits_pretag_slot_and_completes(self, mock_git_repo):
        """A state file written under the OLD ordering (snapshot as a
        post-release step, never recorded) has every step marked EXCEPT
        SNAPSHOT_REGENERATED, with the tag already pushed. Resuming must not
        try to insert a snapshot commit before the existing tag: it forfeits
        the pre-tag slot and marks the step done post-hoc, completing cleanly."""
        from rlsbl.commands.release import resume_cmd
        from rlsbl.commands.release.release_state import (
            get_state_path, load_release_state,
        )
        from test_release_post_steps import (
            _bump_and_tag, _fake_run_factory, _make_ctx, _make_in_progress_state,
            _setup_npm_project,
        )

        _setup_npm_project(mock_git_repo)
        _bump_and_tag(mock_git_repo)
        head_before = git(mock_git_repo, "rev-parse", "HEAD")

        # Old-ordering completed set: everything except the (new) pre-tag
        # SNAPSHOT_REGENERATED slot.
        old_completed = [s for s in RELEASE_STEPS if s != "SNAPSHOT_REGENERATED"]
        state_path = _make_in_progress_state(
            mock_git_repo, completed_steps=old_completed,
        )

        with (
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.run_gh", return_value=""),
            patch("rlsbl.commands.release.run", side_effect=_fake_run_factory()),
        ):
            resume_cmd(
                load_release_state(state_path),
                {"yes": True, "quiet": True},
                ctx=_make_ctx(mock_git_repo),
            )

        # Resume completed and cleared the state file.
        assert not os.path.exists(get_state_path(str(mock_git_repo)))
        # No pre-tag snapshot commit was inserted before the existing tag.
        assert git(mock_git_repo, "rev-parse", "HEAD") == head_before, (
            "the forfeited pre-tag slot must not create a commit before the tag"
        )
