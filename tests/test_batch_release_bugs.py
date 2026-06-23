"""Tests for batch release bugs: redundant validation, dirty tree sources, and release init warning."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from rlsbl.commands.release import _run_cmd_inner
from rlsbl.commands.release_init import run_cmd as release_init_run_cmd
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE


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


# ---------------------------------------------------------------------------
# Phase 1c: release init warns in explicit-mode monorepo
# ---------------------------------------------------------------------------


def _setup_explicit_workspace(tmp_path):
    """Set up a monorepo workspace with [[releasables]] and a pypi project."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    ws_toml = ws_dir / WORKSPACE_FILE
    ws_toml.write_text(
        '[[projects]]\npath = "pkg-a"\n\n'
        '[[releasables]]\nname = "core"\nmembers = ["pkg-a"]\n'
    )
    # Create the project directory with a detectable target
    proj_dir = tmp_path / "pkg-a"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
    )
    return proj_dir


def _setup_implicit_workspace(tmp_path):
    """Set up a monorepo workspace WITHOUT [[releasables]] (implicit mode)."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    ws_toml = ws_dir / WORKSPACE_FILE
    ws_toml.write_text('[[projects]]\npath = "pkg-a"\n')
    proj_dir = tmp_path / "pkg-a"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
    )
    return proj_dir


class TestReleaseInitExplicitModeWarning:
    """rlsbl release init should warn when run inside an explicit-mode monorepo."""

    def test_warns_in_explicit_mode(self, tmp_path, capsys):
        """release init emits a warning when workspace uses [[releasables]]."""
        proj_dir = _setup_explicit_workspace(tmp_path)
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "rlsbl monorepo release-init" in captured.err
        assert "explicit mode" in captured.err

    def test_no_warning_in_implicit_mode(self, tmp_path, capsys):
        """release init does NOT warn when workspace is in implicit mode."""
        proj_dir = _setup_implicit_workspace(tmp_path)
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "explicit mode" not in captured.err

    def test_no_warning_outside_monorepo(self, tmp_path, capsys):
        """release init does NOT warn when not inside a monorepo."""
        proj_dir = tmp_path / "standalone"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "standalone"\nversion = "0.1.0"\n'
        )
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "explicit mode" not in captured.err
