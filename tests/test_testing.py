"""Tests for the shared test-running module rlsbl.testing."""

import json
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.testing import run_project_tests, sync_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_npm_project(tmp_path, test_script=None):
    """Create a minimal npm project with optional test script."""
    pkg = {"name": "test-pkg", "version": "1.0.0"}
    if test_script is not None:
        pkg["scripts"] = {"test": test_script}
    (tmp_path / "package.json").write_text(json.dumps(pkg) + "\n")


# ---------------------------------------------------------------------------
# pypi target
# ---------------------------------------------------------------------------

class TestPypiTarget:
    """Tests for run_project_tests with pypi target."""

    def test_pypi_runs_pytest(self, tmp_project):
        """pypi target runs uv sync + uv run pytest when uv is available."""
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 2
            # First call: uv sync --all-packages --quiet
            assert mock_run.call_args_list[0][0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            # Second call: uv run pytest
            assert mock_run.call_args_list[1][0][0] == ["uv", "run", "pytest"]

    def test_pypi_uv_sync_verbose(self, tmp_project):
        """When uv_sync_verbose is set, uv sync runs without --quiet."""
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi",
                project_dir=str(tmp_project),
                config={"uv_sync_verbose": True},
            )

            assert result is True
            sync_call = mock_run.call_args_list[0][0][0]
            assert sync_call == ["uv", "sync", "--all-packages"]  # no --quiet

    def test_pypi_uv_sync_failure_returns_false(self, tmp_project):
        """When uv sync fails, returns False immediately."""
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is False
            # Only uv sync should have been called (not pytest)
            assert mock_run.call_count == 1

    def test_pypi_fallback_to_bare_pytest(self, tmp_project):
        """When uv is not available but pytest is, falls back to bare pytest."""
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            def tool_side_effect(name, *args, **kwargs):
                if name == "uv":
                    return None
                if name == "pytest":
                    return "/usr/bin/pytest"
                return None

            mock_tool.side_effect = tool_side_effect
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["pytest"]


# ---------------------------------------------------------------------------
# go target
# ---------------------------------------------------------------------------

class TestGoTarget:
    """Tests for run_project_tests with go target."""

    def test_go_runs_go_test(self, tmp_project):
        """go target runs go test ./... -race -short -count=1."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("go", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == [
                "go", "test", "./...", "-race", "-short", "-count=1"
            ]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_go_failure_returns_false(self, tmp_project):
        """When go test fails, returns False."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("go", project_dir=str(tmp_project))

            assert result is False


# ---------------------------------------------------------------------------
# npm target
# ---------------------------------------------------------------------------

class TestNpmTarget:
    """Tests for run_project_tests with npm target."""

    def test_npm_runs_npm_test(self, tmp_project):
        """npm target runs npm test when package.json has a test script."""
        _setup_npm_project(tmp_project, test_script="jest")

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["npm", "test"]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_npm_skips_without_test_script(self, tmp_project):
        """npm target skips when package.json has no test script."""
        _setup_npm_project(tmp_project, test_script=None)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result is True
            mock_run.assert_not_called()

    def test_npm_skips_without_package_json(self, tmp_project):
        """npm target skips when no package.json exists."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result is True
            mock_run.assert_not_called()

    def test_npm_failure_returns_false(self, tmp_project):
        """When npm test fails, returns False."""
        _setup_npm_project(tmp_project, test_script="jest")

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result is False


# ---------------------------------------------------------------------------
# Unknown target
# ---------------------------------------------------------------------------

class TestUnknownTarget:
    """Tests for run_project_tests with unrecognized targets."""

    def test_unknown_target_returns_true(self, tmp_project):
        """Unknown targets return True without running any subprocess."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests("cargo", project_dir=str(tmp_project))

            assert result is True
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# dry_run flag
# ---------------------------------------------------------------------------

class TestDryRun:
    """Tests for dry_run behavior."""

    def test_dry_run_skips_execution(self, tmp_project):
        """When dry_run=True, no subprocess calls are made."""
        _setup_npm_project(tmp_project, test_script="jest")

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests(
                "npm", project_dir=str(tmp_project), dry_run=True
            )

            assert result is True
            mock_run.assert_not_called()

    def test_dry_run_skips_pypi(self, tmp_project):
        """dry_run skips pypi tests too."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), dry_run=True
            )

            assert result is True
            mock_run.assert_not_called()

    def test_dry_run_skips_go(self, tmp_project):
        """dry_run skips go tests too."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests(
                "go", project_dir=str(tmp_project), dry_run=True
            )

            assert result is True
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# workspace_root parameter (monorepo support)
# ---------------------------------------------------------------------------

class TestWorkspaceRoot:
    """Tests for workspace_root parameter: uv sync runs at workspace root."""

    def test_pypi_workspace_syncs_at_workspace_root(self, tmp_project):
        """When workspace_root is set, uv sync runs at workspace_root, pytest at project_dir."""
        workspace = str(tmp_project / "workspace")
        project = str(tmp_project / "workspace" / "pkg-a")

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=project, workspace_root=workspace
            )

            assert result is True
            assert mock_run.call_count == 2
            # uv sync runs at workspace root
            sync_call = mock_run.call_args_list[0]
            assert sync_call[0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            assert sync_call.kwargs.get("cwd") == workspace
            # uv run pytest runs at project dir
            pytest_call = mock_run.call_args_list[1]
            assert pytest_call[0][0] == ["uv", "run", "pytest"]
            assert pytest_call.kwargs.get("cwd") == project

    def test_pypi_workspace_skip_sync(self, tmp_project):
        """When skip_sync is True, uv sync is skipped but pytest still runs."""
        project = str(tmp_project / "pkg-a")

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=project, skip_sync=True
            )

            assert result is True
            # Only pytest should run, not uv sync
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "pytest"]
            assert mock_run.call_args.kwargs.get("cwd") == project

    def test_pypi_without_workspace_root_syncs_at_project_dir(self, tmp_project):
        """Without workspace_root, uv sync runs at project_dir (existing behavior)."""
        project = str(tmp_project)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=project)

            assert result is True
            assert mock_run.call_count == 2
            # Both calls use project_dir
            assert mock_run.call_args_list[0].kwargs.get("cwd") == project
            assert mock_run.call_args_list[1].kwargs.get("cwd") == project


# ---------------------------------------------------------------------------
# sync_workspace
# ---------------------------------------------------------------------------

class TestSyncWorkspace:
    """Tests for the sync_workspace helper."""

    def test_sync_workspace_runs_uv_sync_at_root(self, tmp_project):
        """sync_workspace runs uv sync --all-packages --quiet at the given root."""
        root = str(tmp_project)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = sync_workspace(root)

            assert result is True
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            assert mock_run.call_args.kwargs.get("cwd") == root

    def test_sync_workspace_verbose(self, tmp_project):
        """sync_workspace with verbose=True omits --quiet."""
        root = str(tmp_project)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = sync_workspace(root, verbose=True)

            assert result is True
            assert mock_run.call_args[0][0] == ["uv", "sync", "--all-packages"]

    def test_sync_workspace_failure(self, tmp_project):
        """sync_workspace returns False when uv sync fails."""
        root = str(tmp_project)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = sync_workspace(root)

            assert result is False

    def test_sync_workspace_no_uv(self, tmp_project):
        """sync_workspace returns True when uv is not available."""
        with patch("rlsbl.testing.require_tool") as mock_tool:
            mock_tool.return_value = None

            result = sync_workspace(str(tmp_project))

            assert result is True
