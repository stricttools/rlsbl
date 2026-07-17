"""Behavioral test for the SNAPSHOT_REGENERATED failure path.

Snapshot regeneration now runs BEFORE the tag (so the tag is the pushed
branch tip), which makes it a fatal pre-tag mutating step. A failure aborts
the release and rolls back to the pre-release state -- like its mutating
neighbors -- rather than the old post-push non-fatal behavior (record a
marker and complete). Because the failure happens before TAGGED, the
pre-TAGGED rollback path clears the state file and no tag is created.
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from test_representative_write_elimination import (
    _run_release,
    _setup_releasable_workspace,
)
from rlsbl.commands.release.release_state import get_state_path
from rlsbl.workspace import get_releasable_dir


class TestSnapshotFailureFatal:

    def test_snapshot_failure_aborts_and_rolls_back(self, tmp_project, capsys):
        """A pre-tag snapshot regeneration failure aborts the release: the
        error propagates, the pre-TAGGED rollback clears the state file, and
        no release tag is created."""
        core = _setup_releasable_workspace(tmp_project)

        with pytest.raises(RuntimeError) as exc_info:
            _run_release(
                core, tmp_project,
                extra_patches=(
                    patch(
                        "rlsbl.snapshot.generate_snapshot",
                        side_effect=RuntimeError("disk full"),
                    ),
                ),
            )

        assert "disk full" in str(exc_info.value)

        # Pre-TAGGED rollback clears the releasable state file.
        state_path = get_state_path(
            str(core),
            releasable_dir=get_releasable_dir(str(tmp_project), "alpha"),
        )
        assert not os.path.exists(state_path), (
            "a fatal pre-tag snapshot failure must not preserve release state"
        )

        # No tag was created (the failure happened before TAGGED).
        tags = subprocess.run(
            ["git", "tag", "-l", "alpha@v1.0.1"],
            cwd=str(tmp_project), capture_output=True, text=True,
        ).stdout.strip()
        assert tags == "", "no release tag may exist after a pre-tag abort"
