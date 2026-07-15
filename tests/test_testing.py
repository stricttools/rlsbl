"""Tests for the shared test-running module rlsbl.testing."""

import json
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.errors import ConfigError
from rlsbl.testing import (
    CHECK_TIMEOUT_HINT,
    _probe_pytest_location,
    _resolve_pytest_invocation,
    run_project_tests,
    sync_workspace,
)


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
        """pypi standalone target runs uv run pytest when uv is available."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            # Standalone: no sync, just uv run pytest
            assert mock_run.call_count == 1
            assert mock_run.call_args_list[0][0][0] == ["uv", "run", "python", "-m", "pytest"]

    def test_pypi_workspace_member_syncs_and_runs(self, tmp_project):
        """pypi workspace member runs uv sync + uv run pytest."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), workspace_root=str(ws_root)
            )

            assert result is True
            assert mock_run.call_count == 2
            # First call: uv sync --all-packages --quiet at workspace root
            assert mock_run.call_args_list[0][0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            assert mock_run.call_args_list[0].kwargs.get("cwd") == str(ws_root)
            # Second call: uv run pytest at project dir
            assert mock_run.call_args_list[1][0][0] == ["uv", "run", "python", "-m", "pytest"]

    def test_pypi_uv_sync_verbose(self, tmp_project):
        """When uv_sync_verbose is set, uv sync runs without --quiet (workspace member)."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi",
                project_dir=str(tmp_project),
                workspace_root=str(ws_root),
                config={"uv_sync_verbose": True},
            )

            assert result is True
            sync_call = mock_run.call_args_list[0][0][0]
            assert sync_call == ["uv", "sync", "--all-packages"]  # no --quiet

    def test_pypi_uv_sync_failure_returns_false(self, tmp_project):
        """When uv sync fails (workspace member), returns False immediately."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), workspace_root=str(ws_root)
            )

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
            assert mock_run.call_args[0][0] == ["python", "-m", "pytest"]


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
        """When workspace_root is set and project is a workspace member, uv sync runs at workspace_root."""
        workspace = tmp_project / "workspace"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        project = str(workspace / "pkg-a")
        workspace = str(workspace)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=workspace),
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
            assert pytest_call[0][0] == ["uv", "run", "python", "-m", "pytest"]
            assert pytest_call.kwargs.get("cwd") == project

    def test_pypi_workspace_skip_sync(self, tmp_project):
        """When skip_sync is True, uv sync is skipped but pytest still runs (workspace member)."""
        project = str(tmp_project / "pkg-a")

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(tmp_project)),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=project, workspace_root=str(tmp_project),
                skip_sync=True,
            )

            assert result is True
            # Only pytest should run, not uv sync
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-m", "pytest"]
            assert mock_run.call_args.kwargs.get("cwd") == project

    def test_pypi_standalone_no_sync(self, tmp_project):
        """Standalone project does not run uv sync (uv run handles deps)."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest']\n"
        )
        project = str(tmp_project)

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=project)

            assert result is True
            # No sync call -- only pytest
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-m", "pytest"]
            assert mock_run.call_args.kwargs.get("cwd") == project


# ---------------------------------------------------------------------------
# sync_workspace
# ---------------------------------------------------------------------------

class TestSyncWorkspace:
    """Tests for the sync_workspace helper."""

    def test_sync_workspace_runs_uv_sync_at_root(self, tmp_project):
        """sync_workspace runs uv sync --all-packages --quiet at the given root."""
        root = str(tmp_project)
        (tmp_project / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1.0'\n")

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
        (tmp_project / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1.0'\n")

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
        (tmp_project / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1.0'\n")

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


# ---------------------------------------------------------------------------
# _probe_pytest_location
# ---------------------------------------------------------------------------

class TestProbePytestLocation:
    """Tests for _probe_pytest_location."""

    def test_dependency_group_dev(self, tmp_project):
        """Finds pytest in [dependency-groups].dev."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0", "ruff"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("dependency-group", "dev")

    def test_dependency_group_named(self, tmp_project):
        """Finds pytest in a named dependency group (not dev)."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ntest = ["pytest", "coverage"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("dependency-group", "test")

    def test_optional_dependencies(self, tmp_project):
        """Finds pytest in [project.optional-dependencies]."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[project.optional-dependencies]\ntest = ["pytest>=7.0", "hypothesis"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("optional-dep", "test")

    def test_uv_dev_dependencies(self, tmp_project):
        """Finds pytest in [tool.uv].dev-dependencies."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[tool.uv]\ndev-dependencies = ["pytest>=8.0"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("uv-dev", "dev")

    def test_not_found(self, tmp_project):
        """Returns None when pytest is not declared anywhere."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["ruff", "mypy"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result is None

    def test_no_pyproject(self, tmp_project):
        """Returns None when pyproject.toml does not exist."""
        result = _probe_pytest_location(str(tmp_project))
        assert result is None

    def test_priority_order(self, tmp_project):
        """dependency-groups takes priority over optional-dependencies."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\nci = ["pytest"]\n\n'
            '[project.optional-dependencies]\ntest = ["pytest"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("dependency-group", "ci")

    def test_dict_entries_skipped(self, tmp_project):
        """Dict entries (include groups) in dependency groups are skipped."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[[dependency-groups.dev]]\ninclude-group = "test"\n\n'
            '[dependency-groups]\ntest = ["pytest"]\n'
        )
        # The include-group dict entry should be skipped; "test" group has pytest
        result = _probe_pytest_location(str(tmp_project))
        assert result is not None

    def test_case_insensitive(self, tmp_project):
        """Matches pytest case-insensitively."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["Pytest>=8.0"]\n'
        )
        result = _probe_pytest_location(str(tmp_project))
        assert result == ("dependency-group", "dev")


# ---------------------------------------------------------------------------
# _resolve_pytest_invocation
# ---------------------------------------------------------------------------

class TestResolvePytestInvocation:
    """Tests for _resolve_pytest_invocation."""

    def test_workspace_member(self, tmp_project):
        """Workspace members get plain uv run pytest."""
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value="/ws"):
            result = _resolve_pytest_invocation(str(tmp_project), "/ws")
        assert result == ["uv", "run", "python", "-m", "pytest"]

    def test_standalone_dev_group(self, tmp_project):
        """Standalone with pytest in [dependency-groups].dev gets uv run pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "python", "-m", "pytest"]

    def test_standalone_named_group(self, tmp_project):
        """Standalone with pytest in a named group gets --group flag."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ntest = ["pytest"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "--group", "test", "python", "-m", "pytest"]

    def test_standalone_optional_dep(self, tmp_project):
        """Standalone with pytest in optional-dependencies gets --extra flag."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[project.optional-dependencies]\ntest = ["pytest>=7.0"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "--extra", "test", "python", "-m", "pytest"]

    def test_standalone_uv_dev(self, tmp_project):
        """Standalone with pytest in [tool.uv].dev-dependencies gets uv run pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[tool.uv]\ndev-dependencies = ["pytest"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "python", "-m", "pytest"]

    def test_standalone_not_declared_raises(self, tmp_project):
        """Standalone with no pytest declaration raises ConfigError."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["ruff"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            with pytest.raises(ConfigError, match="pytest is not declared"):
                _resolve_pytest_invocation(str(tmp_project), None)


# ---------------------------------------------------------------------------
# Integration tests: run_project_tests end-to-end flow
# ---------------------------------------------------------------------------

class TestTimeoutHint:
    """Timeout-failure messages name the configurable budget knob.

    Every ``command timed out`` message must append CHECK_TIMEOUT_HINT so an
    agent hitting a timeout learns which knob (check_timeout / RLSBL_CHECK_TIMEOUT)
    controls the budget -- while making clear the check still hard-fails on hangs.
    """

    def _timeout(self, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", ["cmd"]), timeout=1)

    def test_hint_is_grounded(self):
        """The hint names the config key, the env var, and the hard-fail caveat."""
        assert "check_timeout in .rlsbl/config.json" in CHECK_TIMEOUT_HINT
        assert "RLSBL_CHECK_TIMEOUT" in CHECK_TIMEOUT_HINT
        assert "hard-fail" in CHECK_TIMEOUT_HINT

    def test_pypi_uv_timeout_prints_hint(self, tmp_project, capsys):
        """pypi (uv path) timeout message includes the remediation hint."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run", side_effect=self._timeout),
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert result is False
        err = capsys.readouterr().err
        assert "timed out" in err
        assert CHECK_TIMEOUT_HINT in err

    def test_pypi_fallback_timeout_prints_hint(self, tmp_project, capsys):
        """pypi bare-fallback (python -m pytest) timeout includes the hint."""
        def tool_side_effect(name, *args, **kwargs):
            return None if name == "uv" else "/usr/bin/pytest"

        with (
            patch("rlsbl.testing.require_tool", side_effect=tool_side_effect),
            patch("rlsbl.testing.subprocess.run", side_effect=self._timeout),
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert result is False
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_go_timeout_prints_hint(self, tmp_project, capsys):
        """go timeout message includes the remediation hint."""
        with patch("rlsbl.testing.subprocess.run", side_effect=self._timeout):
            result = run_project_tests("go", project_dir=str(tmp_project))

        assert result is False
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_npm_timeout_prints_hint(self, tmp_project, capsys):
        """npm timeout message includes the remediation hint."""
        _setup_npm_project(tmp_project, test_script="jest")
        with patch("rlsbl.testing.subprocess.run", side_effect=self._timeout):
            result = run_project_tests("npm", project_dir=str(tmp_project))

        assert result is False
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_sync_workspace_timeout_prints_hint(self, tmp_project, capsys):
        """sync_workspace timeout message includes the remediation hint."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.subprocess.run", side_effect=self._timeout),
        ):
            result = sync_workspace(str(tmp_project))

        assert result is False
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err


class TestPypiIntegration:
    """Integration tests exercising run_project_tests through _run_pypi_tests."""

    def test_workspace_member_preserves_behavior(self, tmp_project):
        """Workspace member: syncs at workspace root, runs uv run pytest."""
        ws_root = tmp_project / "ws"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        pkg = tmp_project / "ws" / "pkg"
        pkg.mkdir()

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=str(pkg), workspace_root=str(ws_root)
            )

            assert result is True
            assert mock_run.call_count == 2
            sync_cmd = mock_run.call_args_list[0][0][0]
            assert sync_cmd == ["uv", "sync", "--all-packages", "--quiet"]
            assert mock_run.call_args_list[0].kwargs["cwd"] == str(ws_root)
            pytest_cmd = mock_run.call_args_list[1][0][0]
            assert pytest_cmd == ["uv", "run", "python", "-m", "pytest"]
            assert mock_run.call_args_list[1].kwargs["cwd"] == str(pkg)

    def test_standalone_optional_dep_test(self, tmp_project):
        """Non-workspace, pytest in [project.optional-dependencies].test: uv run --extra test pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[project.optional-dependencies]\ntest = ["pytest>=8.0"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "--extra", "test", "python", "-m", "pytest"]

    def test_standalone_dev_group_default(self, tmp_project):
        """Non-workspace, pytest in [dependency-groups].dev: uv run pytest (default dev)."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0", "ruff"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-m", "pytest"]

    def test_standalone_not_declared_raises(self, tmp_project):
        """Non-workspace, pytest not declared anywhere: raises ConfigError."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["ruff", "mypy"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
        ):
            mock_tool.return_value = "/usr/bin/uv"

            with pytest.raises(ConfigError, match="pytest is not declared"):
                run_project_tests("pypi", project_dir=str(tmp_project))

    def test_standalone_named_group(self, tmp_project):
        """Non-workspace, pytest in named dependency group: uv run --group <name> pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ntesting = ["pytest>=8.0"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.testing.subprocess.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "--group", "testing", "python", "-m", "pytest"]
