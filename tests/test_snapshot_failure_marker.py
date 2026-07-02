"""Behavioral test for the SNAPSHOT_REGENERATED failure path.

Snapshot regeneration after a monorepo release is non-fatal: a failure is
recorded as a failure marker and loudly named in the completion summary,
but the release completes and the state file is cleared (mirrors the
tested deploy/post-hook non-fatal pattern).
"""

import os
from unittest.mock import patch

import pytest

from test_representative_write_elimination import (
    _run_release,
    _setup_releasable_workspace,
)
from rlsbl.commands.release.release_state import get_state_path
from rlsbl.workspace import get_releasable_dir


class TestSnapshotFailureNonFatal:

    def test_snapshot_failure_recorded_and_named_in_summary(
        self, tmp_project, capsys,
    ):
        """A snapshot regeneration failure does not abort the release: the
        state is cleared, but the summary loudly names SNAPSHOT_REGENERATED."""
        core = _setup_releasable_workspace(tmp_project)

        # Must NOT raise: snapshot failures are non-fatal
        _run_release(
            core, tmp_project,
            extra_patches=(
                patch(
                    "rlsbl.snapshot.generate_snapshot",
                    side_effect=RuntimeError("disk full"),
                ),
            ),
        )

        # Release completed: state cleared at the releasable location
        state_path = get_state_path(
            str(core),
            releasable_dir=get_releasable_dir(str(tmp_project), "alpha"),
        )
        assert not os.path.exists(state_path), (
            "non-fatal snapshot failure must not preserve release state"
        )

        captured = capsys.readouterr()
        assert "SNAPSHOT_REGENERATED" in captured.err
        assert "disk full" in captured.err
