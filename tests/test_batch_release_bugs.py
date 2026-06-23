"""Tests for batch release bugs: redundant validation, dirty tree sources, and release init warning."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from rlsbl.commands.release import _run_cmd_inner
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


# ---------------------------------------------------------------------------
# Bug 1: _run_cmd_inner should skip environment validation in batch mode
# ---------------------------------------------------------------------------


class TestBatchModeSkipsValidation:
    """validate_gh_cli, validate_clean_tree, and validate_branch_and_remote
    should NOT be called when batch-mode is True, because the batch
    orchestrator already validated them upfront."""

    def _make_ctx(self, tmp_path):
        """Create a minimal ProjectContext for testing."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        # Need .rlsbl/config.json for the release flow
        rlsbl_dir = project_root / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"private": true}')
        return ProjectContext(
            project_root=project_root,
            workspace_root=None,
            config={"private": True},
        )

    def _make_release_config(self):
        """Create a minimal ReleaseConfig."""
        return ReleaseConfig(
            bump="patch",
            description="test release",
            include=["pypi"],
            exclude=[],
        )

    @patch("rlsbl.commands.release.validate_branch_and_remote")
    @patch("rlsbl.commands.release.validate_clean_tree")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets")
    def test_batch_mode_skips_env_validation(
        self,
        mock_validate_targets,
        mock_validate_ota,
        mock_validate_config,
        mock_validate_pipeline,
        mock_validate_gh,
        mock_validate_clean,
        mock_validate_branch,
        tmp_path,
    ):
        """When batch-mode=True, the three environment validators must not be called."""
        ctx = self._make_ctx(tmp_path)
        rc = self._make_release_config()
        flags = {"batch-mode": True, "quiet": True}

        # We expect _run_cmd_inner to proceed past validation and fail somewhere
        # later (e.g., resolving monorepo context or computing version).
        # That's fine -- we only care that the three validators are NOT called.
        mock_validate_targets.return_value = set()

        with pytest.raises(Exception):
            # Will fail at some later point; we just need to verify the mocks
            _run_cmd_inner(rc, flags, ctx=ctx)

        mock_validate_gh.assert_not_called()
        mock_validate_clean.assert_not_called()
        mock_validate_branch.assert_not_called()

    @patch("rlsbl.commands.release.validate_branch_and_remote")
    @patch("rlsbl.commands.release.validate_clean_tree")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets")
    def test_non_batch_mode_calls_env_validation(
        self,
        mock_validate_targets,
        mock_validate_ota,
        mock_validate_config,
        mock_validate_pipeline,
        mock_validate_gh,
        mock_validate_clean,
        mock_validate_branch,
        tmp_path,
    ):
        """When batch-mode is not set, the three environment validators MUST be called."""
        ctx = self._make_ctx(tmp_path)
        rc = self._make_release_config()
        flags = {"quiet": True}

        mock_validate_targets.return_value = set()
        mock_validate_clean.return_value = set()
        mock_validate_branch.return_value = "main"

        with pytest.raises(Exception):
            _run_cmd_inner(rc, flags, ctx=ctx)

        mock_validate_gh.assert_called_once()
        mock_validate_clean.assert_called_once()
        mock_validate_branch.assert_called_once()
