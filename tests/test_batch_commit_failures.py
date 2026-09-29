"""A batch commit that fails stops the batch, and the re-run it names finishes it.

The batch's own commits -- the resolved plan, the archive of a completed plan,
and the archive of the batch release file -- were made with failures reduced
to a warning. A plan commit that failed left the batch releasing against an
uncommitted plan; a failed archive commit printed success while nothing
recorded the archive. Each is fatal now: the files the step wrote or renamed
are put back, and the error names the re-run, which these tests perform.
"""

import os
import subprocess
from unittest.mock import patch

import pytest
from githarness import git

from rlsbl.commands.monorepo import batch_release
from rlsbl.commands.monorepo.batch_plan import PLAN_FILENAME
from rlsbl.commands.release.validate import ReleaseValidationError

from test_batch_finalize_push import _archived_batch_file, _remote_tags_land
from test_batch_main_as_candidate import _PushRecorder, _run_batch, _setup_batch_workspace


REAL_COMMIT = batch_release.commit_files


def _failing_commit_for(prefix):
    """commit_files, failing the commit whose message starts with *prefix*."""
    def commit(message, files, **kwargs):
        if message.startswith(prefix):
            raise subprocess.CalledProcessError(
                1, "safegit", stderr="fatal: index.lock exists",
            )
        return REAL_COMMIT(message, files, **kwargs)
    return commit


def _releases(root):
    return root / ".rlsbl-monorepo" / "releases"


def _batch(root, commit=None):
    recorder = _PushRecorder(root, git(root, "rev-parse", "HEAD"))
    with _remote_tags_land(), patch.object(
        batch_release, "commit_files", commit or REAL_COMMIT,
    ):
        _run_batch(root, ci_return=("green", []), push_side_effect=recorder)


def _tags(root):
    return git(root, "tag", "-l").split()


def test_a_failed_plan_commit_releases_nothing_and_the_rerun_does(tmp_project):
    _setup_batch_workspace(tmp_project)
    head = git(tmp_project, "rev-parse", "HEAD")
    tags_before = _tags(tmp_project)

    with pytest.raises(ReleaseValidationError) as exc:
        _batch(tmp_project, _failing_commit_for("chore: resolve batch release plan"))
    assert "index.lock exists" in str(exc.value)
    assert "re-run `rlsbl monorepo release run`" in str(exc.value)
    assert not (_releases(tmp_project) / PLAN_FILENAME).exists()
    assert git(tmp_project, "rev-parse", "HEAD") == head
    assert _tags(tmp_project) == tags_before

    _batch(tmp_project)
    assert len(_tags(tmp_project)) > len(tags_before)
    assert len(_archived_batch_file(tmp_project)) == 1


def test_a_failed_finalize_commit_restores_the_batch_file(tmp_project, capsys):
    _setup_batch_workspace(tmp_project)

    with pytest.raises(ReleaseValidationError) as exc:
        _batch(tmp_project, _failing_commit_for("chore: finalize batch release file"))
    message = str(exc.value)
    assert "index.lock exists" in message
    assert "every item of the batch is released" in message
    assert (_releases(tmp_project) / "unreleased.toml").exists()
    assert (_releases(tmp_project) / PLAN_FILENAME).exists()
    assert _archived_batch_file(tmp_project) == []
    assert git(tmp_project, "status", "--porcelain", "--", ".rlsbl-monorepo/releases") == ""

    # The named re-run: it meets the completed plan of the batch that file
    # ran, and finishes the archive the failed commit did not record.
    with pytest.raises(SystemExit) as done:
        _batch(tmp_project)
    assert done.value.code == 0
    assert "Finished the archive" in capsys.readouterr().out
    assert not (_releases(tmp_project) / "unreleased.toml").exists()
    assert not (_releases(tmp_project) / PLAN_FILENAME).exists()
    assert len(_archived_batch_file(tmp_project)) == 1
    assert any(
        f.endswith(".plan.json") for f in os.listdir(_releases(tmp_project))
    )
    assert git(tmp_project, "status", "--porcelain", "--", ".rlsbl-monorepo/releases") == ""


def test_a_failed_completed_plan_archive_restores_the_plan(tmp_project, capsys):
    _setup_batch_workspace(tmp_project)
    with pytest.raises(ReleaseValidationError):
        _batch(tmp_project, _failing_commit_for("chore: finalize batch release file"))
    capsys.readouterr()

    # A batch file edited after the batch ran is the next batch's: the re-run
    # archives only the completed plan, and its commit fails here.
    batch_file = _releases(tmp_project) / "unreleased.toml"
    batch_file.write_text(batch_file.read_text() + "# the next batch\n")
    git(tmp_project, "commit", "-q", "-am", "next batch")
    with pytest.raises(SystemExit):
        _batch(tmp_project, _failing_commit_for("chore: archive completed batch plan"))
    err = capsys.readouterr().err
    assert "index.lock exists" in err and "re-run `rlsbl monorepo release run`" in err
    plan = _releases(tmp_project) / PLAN_FILENAME
    assert plan.exists()
    assert git(tmp_project, "status", "--porcelain", "--", ".rlsbl-monorepo/releases") == ""

    with pytest.raises(SystemExit):
        _batch(tmp_project)
    assert "is a completed plan" in capsys.readouterr().err
    assert not plan.exists()
    assert batch_file.exists()
    assert _archived_batch_file(tmp_project) == []
