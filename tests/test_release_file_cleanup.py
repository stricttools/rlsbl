"""Red tests for the empty release file bug.

Bug: After finalization, both ``_finalize_batch_file`` (batch_release.py)
and the standalone release finalization (execute.py) create an empty
``unreleased.toml`` file.  An empty file with no TOML content is not a
valid state -- it should not exist at all after finalization.  The next
``release init`` creates it fresh when needed.

These tests are expected to FAIL on the current code (red phase).
"""

import os
import stat
from unittest.mock import patch

import pytest


class TestBatchFinalizeDoesNotCreateEmptyFile:
    """_finalize_batch_file should NOT create an empty unreleased.toml."""

    def test_batch_finalize_does_not_create_empty_file(self, tmp_path):
        """After batch finalization, unreleased.toml should not exist.

        Current behavior (buggy): creates an empty unreleased.toml.
        Expected behavior: no unreleased.toml after finalization.
        """
        from rlsbl.commands.monorepo.batch_release import _finalize_batch_file

        # Set up the releases directory with an unreleased.toml
        releases_dir = tmp_path / ".rlsbl-monorepo" / "releases"
        releases_dir.mkdir(parents=True)
        batch_path = releases_dir / "unreleased.toml"
        batch_path.write_text('[releasables.core]\nbump = "patch"\n')

        log_messages = []

        # Mock commit_files since we have no git repo
        with patch(
            "rlsbl.commands.monorepo.batch_release.commit_files"
        ):
            _finalize_batch_file(str(batch_path), log_messages.append)

        # The versioned batch file should exist (renamed from unreleased.toml)
        versioned_files = [
            f for f in os.listdir(str(releases_dir))
            if f.startswith("batch-") and f.endswith(".toml")
        ]
        assert len(versioned_files) == 1, (
            f"Expected exactly 1 versioned batch file, got: {versioned_files}"
        )

        # The empty unreleased.toml should NOT exist
        assert not batch_path.exists(), (
            "unreleased.toml should not exist after finalization, "
            "but an empty file was created"
        )


class TestStandaloneFinalizeDoesNotCreateEmptyFile:
    """The standalone release finalization in execute.py should NOT create
    an empty unreleased.toml.

    The finalization code is inline in the release flow (execute.py ~line 782),
    not in a standalone function. This test reads the source code to verify
    the pattern: after os.rename, the code should NOT re-create the file."""

    def test_standalone_finalize_does_not_create_empty_file(self):
        """The standalone finalization code in execute.py must not create an
        empty unreleased.toml after renaming it to the versioned name.

        Current behavior (buggy): lines 787-789 of execute.py open the
        original path for writing immediately after os.rename, creating
        an empty file.

        Expected behavior: no empty file creation after rename.

        This test inspects the actual source to detect the buggy pattern,
        since the finalization logic is inline and cannot be called in
        isolation without running the full release flow.
        """
        import inspect
        from rlsbl.commands.release import execute

        source = inspect.getsource(execute)

        # Find the finalization block: after "Finalize release file" comment,
        # look for the pattern of os.rename followed by creating an empty file
        # The buggy code does:
        #   os.rename(release_file_path, versioned_release)
        #   ...
        #   with open(release_file_path, "w", ...) as f:
        #       pass  # empty file
        #
        # After the fix, there should be no re-creation of release_file_path

        # Find the finalization section
        finalize_marker = "Finalize release file"
        assert finalize_marker in source, (
            f"Could not find '{finalize_marker}' in execute.py source"
        )

        # Extract the finalization block (from marker to a generous window)
        marker_idx = source.index(finalize_marker)
        finalize_section = source[marker_idx:marker_idx + 1200]

        # The bug: after os.rename, the code opens the original path to create
        # an empty file. This should not happen.
        has_empty_file_creation = (
            "open(release_file_path" in finalize_section
            and "pass  # empty file" in finalize_section
        )

        assert not has_empty_file_creation, (
            "execute.py finalization creates an empty unreleased.toml after "
            "renaming (lines ~787-789). The file should not be recreated -- "
            "release init creates it fresh when needed."
        )
