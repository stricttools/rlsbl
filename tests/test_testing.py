"""Tests for the shared test-running module rlsbl.testing."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from rlsbl.errors import ConfigError
from rlsbl.overlay_state import OverlayModeConflictError
from rlsbl.testing import (
    CHECK_TIMEOUT_HINT,
    _probe_pytest_location,
    _resolve_pytest_invocation,
    collect_active_overlays,
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result.passed
            # Standalone: no sync, just uv run pytest
            assert mock_run.call_count == 1
            assert mock_run.call_args_list[0][0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]

    def test_pypi_workspace_member_syncs_and_runs(self, tmp_project):
        """pypi workspace member runs uv sync + uv run pytest."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), workspace_root=str(ws_root)
            )

            assert result.passed
            assert mock_run.call_count == 2
            # First call: uv sync --all-packages --quiet at workspace root
            assert mock_run.call_args_list[0][0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            assert mock_run.call_args_list[0].kwargs.get("cwd") == str(ws_root)
            # Second call: uv run pytest at project dir
            assert mock_run.call_args_list[1][0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]

    def test_pypi_uv_sync_verbose(self, tmp_project):
        """When uv_sync_verbose is set, uv sync runs without --quiet (workspace member)."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi",
                project_dir=str(tmp_project),
                workspace_root=str(ws_root),
                config={"uv_sync_verbose": True},
            )

            assert result.passed
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), workspace_root=str(ws_root)
            )

            assert not result.passed
            # Only uv sync should have been called (not pytest)
            assert mock_run.call_count == 1

    def test_pypi_fallback_to_bare_pytest(self, tmp_project):
        """When uv is not available but pytest is, falls back to bare pytest."""
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.effects.run") as mock_run,
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

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["python", "-P", "-m", "pytest"]

    def test_pypi_no_uv_no_pytest_fails(self, tmp_project):
        """Neither uv nor pytest installed -> hard fail (no silent skip)."""
        with (
            patch("rlsbl.testing.require_tool", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert not result.passed
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# pypi test.pypi.markers config
# ---------------------------------------------------------------------------

class TestPypiMarkers:
    """The test.pypi.markers config appends -m <markers> to every pytest path."""

    MARKERS_CONFIG = {"test": {"pypi": {"markers": "not integration"}}}

    def test_standalone_dev_group_appends_markers(self, tmp_project):
        """Standalone dev-group path: uv run python -m pytest -m <markers>."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), config=self.MARKERS_CONFIG
            )
            assert result.passed
            assert mock_run.call_args[0][0] == [
                "uv", "run", "python", "-P", "-m", "pytest", "-m", "not integration"
            ]

    def test_standalone_named_group_appends_markers(self, tmp_project):
        """Standalone named-group path preserves --group and appends markers."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ntesting = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), config=self.MARKERS_CONFIG
            )
            assert result.passed
            assert mock_run.call_args[0][0] == [
                "uv", "run", "--group", "testing", "python", "-P", "-m", "pytest",
                "-m", "not integration",
            ]

    def test_standalone_optional_dep_appends_markers(self, tmp_project):
        """Standalone extra path preserves --extra and appends markers."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[project.optional-dependencies]\ntest = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), config=self.MARKERS_CONFIG
            )
            assert result.passed
            assert mock_run.call_args[0][0] == [
                "uv", "run", "--extra", "test", "python", "-P", "-m", "pytest",
                "-m", "not integration",
            ]

    def test_workspace_member_appends_markers(self, tmp_project):
        """Workspace-member path appends markers to the pytest call (not the sync)."""
        ws_root = tmp_project / "workspace"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), workspace_root=str(ws_root),
                config=self.MARKERS_CONFIG,
            )
            assert result.passed
            assert mock_run.call_count == 2
            # sync call unaffected
            assert mock_run.call_args_list[0][0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            # pytest call carries the markers
            assert mock_run.call_args_list[1][0][0] == [
                "uv", "run", "python", "-P", "-m", "pytest", "-m", "not integration"
            ]

    def test_fallback_bare_pytest_appends_markers(self, tmp_project):
        """The bare python -m pytest fallback also carries the markers."""
        def tool_side_effect(name, *args, **kwargs):
            return None if name == "uv" else "/usr/bin/pytest"

        with (
            patch("rlsbl.testing.require_tool", side_effect=tool_side_effect),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(tmp_project), config=self.MARKERS_CONFIG
            )
            assert result.passed
            assert mock_run.call_args[0][0] == [
                "python", "-P", "-m", "pytest", "-m", "not integration"
            ]

    def test_absent_test_section_is_byte_identical(self, tmp_project):
        """No test section -> command is exactly today's, no -m appended."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest>=8.0']\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests("pypi", project_dir=str(tmp_project), config={})
            assert result.passed
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]


# ---------------------------------------------------------------------------
# Dev overlays: the test-suite runner must not wipe them
# ---------------------------------------------------------------------------

def _declare_overlay(root, package="depa", *, sentinel_path=None, declare=True, sentinel=True):
    """Put a project into overlay mode: a declaration, a matching sentinel, and
    the checkout they both name. Returns the checkout path."""
    checkout = root / f"{package}-src"
    checkout.mkdir(parents=True, exist_ok=True)
    if declare:
        (root / "dev-sources.toml.local-only").write_text(
            f'[[overlay]]\npackage = "{package}"\npath = "{checkout}"\n'
        )
    if sentinel:
        (root / "dev-overlays-state.toml.local-only").write_text(
            f'[[overlay]]\npackage = "{package}"\n'
            f'path = "{sentinel_path or checkout}"\nversion = "0.3.1"\n'
        )
    return checkout


class TestDevOverlayPreservation:
    """A declared-and-installed overlay must survive the test-suite check.

    `rlsbl dev sync` overlays sibling checkouts by syncing with `--inexact
    --no-install-package <pkg>`; the sandboxed runner does exactly the same and
    then runs the suite with `uv run --no-sync`. The non-sandboxed runner must
    match that, or it silently reinstalls the locked registry wheel over the
    overlay -- destroying the state the `dev-overlay-drift` check then fails on.
    """

    def _pyproject(self, root):
        (root / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ndev = ['pytest>=8.0']\n"
        )

    def test_workspace_sync_excludes_overlaid_package(self, tmp_project):
        """Workspace member: the sync keeps the overlay and the suite runs with
        --no-sync so `uv run` cannot re-sync it away."""
        ws_root = tmp_project / "ws"
        ws_root.mkdir()
        (ws_root / "pyproject.toml").write_text("[project]\nname = 'ws'\nversion = '0.1.0'\n")
        pkg = ws_root / "pkg"
        pkg.mkdir()
        _declare_overlay(pkg)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(ws_root)),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests(
                "pypi", project_dir=str(pkg), workspace_root=str(ws_root)
            )

        assert result.passed
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == [
            "uv", "sync", "--all-packages", "--quiet",
            "--inexact", "--no-install-package", "depa",
        ]
        assert mock_run.call_args_list[1][0][0] == [
            "uv", "run", "--no-sync", "python", "-P", "-m", "pytest",
        ]

    def test_standalone_syncs_inexact_then_runs_without_sync(self, tmp_project):
        """Standalone: `uv run` would auto-sync (exact) and wipe the overlay, so
        the environment is synced explicitly with the overlay excluded first."""
        self._pyproject(tmp_project)
        _declare_overlay(tmp_project)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert result.passed
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == [
            "uv", "sync", "--inexact", "--quiet", "--no-install-package", "depa",
        ]
        assert mock_run.call_args_list[0].kwargs.get("cwd") == str(tmp_project)
        assert mock_run.call_args_list[1][0][0] == [
            "uv", "run", "--no-sync", "python", "-P", "-m", "pytest",
        ]

    def test_standalone_named_group_carries_selector_into_sync(self, tmp_project):
        """The explicit sync installs the same groups the suite runs with."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n\n"
            "[dependency-groups]\ntesting = ['pytest>=8.0']\n"
        )
        _declare_overlay(tmp_project)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert result.passed
        assert mock_run.call_args_list[0][0][0] == [
            "uv", "sync", "--inexact", "--quiet", "--group", "testing",
            "--no-install-package", "depa",
        ]
        assert mock_run.call_args_list[1][0][0] == [
            "uv", "run", "--no-sync", "--group", "testing",
            "python", "-P", "-m", "pytest",
        ]

    def test_standalone_sync_failure_returns_false(self, tmp_project):
        """A failed overlay-preserving sync fails the check; pytest never runs."""
        self._pyproject(tmp_project)
        _declare_overlay(tmp_project)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert not result.passed
        assert mock_run.call_count == 1

    def test_no_overlay_files_is_byte_identical(self, tmp_project):
        """Registry mode (no local-only files) runs exactly today's commands."""
        self._pyproject(tmp_project)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert result.passed
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]

    def test_declared_but_never_synced_is_a_hard_error(self, tmp_project, capsys):
        """A declaration with no sentinel is neither mode: hard error, and no
        subprocess runs at all -- never a silent sync that wipes the checkout."""
        self._pyproject(tmp_project)
        _declare_overlay(tmp_project, sentinel=False)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert not result.passed
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "dev-sources.toml.local-only" in err
        assert "dev-overlays-state.toml.local-only" in err

    def test_declaration_and_sentinel_path_disagreement_is_a_hard_error(
        self, tmp_project, capsys
    ):
        """Declared at one path, synced from another: hard error, nothing runs."""
        self._pyproject(tmp_project)
        other = tmp_project / "elsewhere"
        other.mkdir()
        _declare_overlay(tmp_project, sentinel_path=other)

        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert not result.passed
        mock_run.assert_not_called()
        assert "depa" in capsys.readouterr().err

    def test_collect_unions_overlays_across_projects(self, tmp_project):
        """A workspace sync must exclude every member's overlaid packages."""
        a, b = tmp_project / "a", tmp_project / "b"
        a.mkdir()
        b.mkdir()
        ca = _declare_overlay(a, "depa")
        cb = _declare_overlay(b, "depb")
        assert collect_active_overlays([str(a), str(b)]) == [
            {"package": "depa", "path": str(ca)},
            {"package": "depb", "path": str(cb)},
        ]

    def test_collect_same_package_two_checkouts_is_a_hard_error(self, tmp_project):
        """One shared environment cannot hold two checkouts of one package."""
        a, b = tmp_project / "a", tmp_project / "b"
        a.mkdir()
        b.mkdir()
        _declare_overlay(a, "depa")
        _declare_overlay(b, "depa")
        with pytest.raises(OverlayModeConflictError, match="depa"):
            collect_active_overlays([str(a), str(b)])

    def test_collect_registry_mode_is_empty(self, tmp_project):
        assert collect_active_overlays([str(tmp_project)]) == []

    def test_go_target_ignores_overlays(self, tmp_project):
        """Overlay mode is a Python-environment concept: the go runner is
        untouched by the files being present."""
        _declare_overlay(tmp_project)
        with patch("rlsbl.effects.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = run_project_tests("go", project_dir=str(tmp_project))

        assert result.passed
        assert mock_run.call_args[0][0] == [
            "go", "test", "./...", "-race", "-short", "-count=1"
        ]


# ---------------------------------------------------------------------------
# go target
# ---------------------------------------------------------------------------

class TestGoTarget:
    """Tests for run_project_tests with go target."""

    def test_go_runs_go_test(self, tmp_project):
        """go target runs go test ./... -race -short -count=1."""
        with patch("rlsbl.effects.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("go", project_dir=str(tmp_project))

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == [
                "go", "test", "./...", "-race", "-short", "-count=1"
            ]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_go_failure_returns_false(self, tmp_project):
        """When go test fails, returns False."""
        with patch("rlsbl.effects.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("go", project_dir=str(tmp_project))

            assert not result.passed


# ---------------------------------------------------------------------------
# npm target
# ---------------------------------------------------------------------------

class TestNpmTarget:
    """Tests for run_project_tests with npm target."""

    def test_npm_runs_npm_test(self, tmp_project):
        """npm target runs npm test when package.json has a test script."""
        _setup_npm_project(tmp_project, test_script="jest")

        with patch("rlsbl.effects.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["npm", "test"]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_npm_skips_without_test_script(self, tmp_project):
        """npm target skips when package.json has no test script."""
        _setup_npm_project(tmp_project, test_script=None)

        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert result.passed
            mock_run.assert_not_called()

    def test_npm_no_package_json_fails(self, tmp_project):
        """npm target hard-fails when no package.json exists (broken declaration)."""
        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert not result.passed
            mock_run.assert_not_called()

    def test_npm_corrupt_package_json_fails(self, tmp_project):
        """npm target hard-fails when package.json is unreadable."""
        (tmp_project / "package.json").write_text("{ this is not json")
        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert not result.passed
            mock_run.assert_not_called()

    def test_npm_missing_tool_fails(self, tmp_project):
        """npm not installed (FileNotFoundError) -> hard fail, not an exception."""
        _setup_npm_project(tmp_project, test_script="jest")
        with patch("rlsbl.effects.run", side_effect=FileNotFoundError("npm")):
            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert not result.passed

    def test_npm_failure_returns_false(self, tmp_project):
        """When npm test fails, returns False."""
        _setup_npm_project(tmp_project, test_script="jest")

        with patch("rlsbl.effects.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("npm", project_dir=str(tmp_project))

            assert not result.passed


# ---------------------------------------------------------------------------
# maven target
# ---------------------------------------------------------------------------

class TestMavenTarget:
    """Tests for run_project_tests with maven target."""

    def test_maven_no_build_file_fails(self, tmp_project):
        """maven target with neither gradlew nor pom.xml -> hard fail
        (a maven target was declared, so a missing manifest is broken, not n/a)."""
        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert not result.passed
            mock_run.assert_not_called()

    def test_maven_mvn_missing_tool_fails(self, tmp_project):
        """mvn not installed (FileNotFoundError) -> hard fail, not an exception."""
        (tmp_project / "pom.xml").write_text("<project></project>\n")
        with patch("rlsbl.effects.run", side_effect=FileNotFoundError("mvn")):
            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert not result.passed


# ---------------------------------------------------------------------------
# Unknown target
# ---------------------------------------------------------------------------

class TestUnknownTarget:
    """Tests for run_project_tests with targets that have no runner.

    This class used to assert that such a target returned True. It did, and
    that was the bug: a release of a project whose target ships no test runner
    recorded a PASSING test step for a suite that never ran. The runner now
    answers SKIPPED, naming what it could not run.
    """

    def test_a_name_that_is_not_a_target_skips_and_names_itself(self, tmp_project):
        """A name outside the registry cannot report a pass."""
        from rlsbl.targets.outcomes import SuiteRunStatus

        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("cargo", project_dir=str(tmp_project))

            assert result.status is SuiteRunStatus.SKIPPED
            assert not result.passed
            assert "cargo" in result.message
            mock_run.assert_not_called()

    def test_a_registered_target_without_a_runner_skips(self, tmp_project):
        """zig is a real target; it just has no built-in test command."""
        from rlsbl.targets.outcomes import SuiteRunStatus

        with patch("rlsbl.effects.run") as mock_run:
            result = run_project_tests("zig", project_dir=str(tmp_project))

            assert result.status is SuiteRunStatus.SKIPPED
            assert not result.passed
            assert "zig" in result.message
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# dry_run flag
# ---------------------------------------------------------------------------

class TestNoDryRunParameter:
    """The runner has no dry-run switch of its own, deliberately.

    It is reached through the impure ``test-suite`` check, which the check
    framework lists rather than runs under ``--dry-run``. The parameter this
    function used to carry was never passed by any caller, and a second
    hand-rolled skip was one more place for the two answers to disagree.
    """

    def test_the_signature_carries_no_dry_run(self):
        import inspect

        assert "dry_run" not in inspect.signature(run_project_tests).parameters

    def test_passing_one_is_a_hard_error(self, tmp_project):
        with pytest.raises(TypeError):
            run_project_tests("npm", project_dir=str(tmp_project), dry_run=True)


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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=project, workspace_root=workspace
            )

            assert result.passed
            assert mock_run.call_count == 2
            # uv sync runs at workspace root
            sync_call = mock_run.call_args_list[0]
            assert sync_call[0][0] == ["uv", "sync", "--all-packages", "--quiet"]
            assert sync_call.kwargs.get("cwd") == workspace
            # uv run pytest runs at project dir
            pytest_call = mock_run.call_args_list[1]
            assert pytest_call[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]
            assert pytest_call.kwargs.get("cwd") == project

    def test_pypi_workspace_skip_sync(self, tmp_project):
        """When skip_sync is True, uv sync is skipped but pytest still runs (workspace member)."""
        project = str(tmp_project / "pkg-a")

        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=str(tmp_project)),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=project, workspace_root=str(tmp_project),
                skip_sync=True,
            )

            assert result.passed
            # Only pytest should run, not uv sync
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=project)

            assert result.passed
            # No sync call -- only pytest
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]
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
            patch("rlsbl.effects.run") as mock_run,
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
            patch("rlsbl.effects.run") as mock_run,
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = sync_workspace(root)

            assert result is False

    def test_sync_workspace_no_uv_fails(self, tmp_project):
        """sync_workspace hard-fails when uv is not available."""
        with patch("rlsbl.testing.require_tool") as mock_tool:
            mock_tool.return_value = None

            result = sync_workspace(str(tmp_project))

            assert result is False


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
        assert result == ["uv", "run", "python", "-P", "-m", "pytest"]

    def test_standalone_dev_group(self, tmp_project):
        """Standalone with pytest in [dependency-groups].dev gets uv run pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "python", "-P", "-m", "pytest"]

    def test_standalone_named_group(self, tmp_project):
        """Standalone with pytest in a named group gets --group flag."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ntest = ["pytest"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "--group", "test", "python", "-P", "-m", "pytest"]

    def test_standalone_optional_dep(self, tmp_project):
        """Standalone with pytest in optional-dependencies gets --extra flag."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[project.optional-dependencies]\ntest = ["pytest>=7.0"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "--extra", "test", "python", "-P", "-m", "pytest"]

    def test_standalone_uv_dev(self, tmp_project):
        """Standalone with pytest in [tool.uv].dev-dependencies gets uv run pytest."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[tool.uv]\ndev-dependencies = ["pytest"]\n'
        )
        with patch("rlsbl.testing.detect_uv_workspace_root", return_value=None):
            result = _resolve_pytest_invocation(str(tmp_project), None)
        assert result == ["uv", "run", "python", "-P", "-m", "pytest"]

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
    agent hitting a timeout learns which knob (the ``check_timeout`` config key)
    controls the budget -- while making clear the check still hard-fails on hangs.
    """

    def _timeout(self, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", ["cmd"]), timeout=1)

    def test_hint_is_grounded(self):
        """The hint names the config key and the hard-fail caveat -- no env var."""
        assert "check_timeout key in .rlsbl/config.json" in CHECK_TIMEOUT_HINT
        assert "RLSBL_" not in CHECK_TIMEOUT_HINT
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
            patch("rlsbl.effects.run", side_effect=self._timeout),
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert not result.passed
        err = capsys.readouterr().err
        assert "timed out" in err
        assert CHECK_TIMEOUT_HINT in err

    def test_pypi_fallback_timeout_prints_hint(self, tmp_project, capsys):
        """pypi bare-fallback (python -m pytest) timeout includes the hint."""
        def tool_side_effect(name, *args, **kwargs):
            return None if name == "uv" else "/usr/bin/pytest"

        with (
            patch("rlsbl.testing.require_tool", side_effect=tool_side_effect),
            patch("rlsbl.effects.run", side_effect=self._timeout),
        ):
            result = run_project_tests("pypi", project_dir=str(tmp_project))

        assert not result.passed
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_go_timeout_prints_hint(self, tmp_project, capsys):
        """go timeout message includes the remediation hint."""
        with patch("rlsbl.effects.run", side_effect=self._timeout):
            result = run_project_tests("go", project_dir=str(tmp_project))

        assert not result.passed
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_maven_gradlew_timeout_prints_hint(self, tmp_project, capsys):
        """maven (gradlew path) timeout message includes the remediation hint."""
        gradlew = tmp_project / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        with patch("rlsbl.effects.run", side_effect=self._timeout):
            result = run_project_tests("maven", project_dir=str(tmp_project))

        assert not result.passed
        err = capsys.readouterr().err
        assert "timed out" in err
        assert CHECK_TIMEOUT_HINT in err

    def test_maven_mvn_timeout_prints_hint(self, tmp_project, capsys):
        """maven (mvn/pom.xml path) timeout message includes the remediation hint."""
        (tmp_project / "pom.xml").write_text("<project></project>\n")
        with patch("rlsbl.effects.run", side_effect=self._timeout):
            result = run_project_tests("maven", project_dir=str(tmp_project))

        assert not result.passed
        err = capsys.readouterr().err
        assert "timed out" in err
        assert CHECK_TIMEOUT_HINT in err

    def test_npm_timeout_prints_hint(self, tmp_project, capsys):
        """npm timeout message includes the remediation hint."""
        _setup_npm_project(tmp_project, test_script="jest")
        with patch("rlsbl.effects.run", side_effect=self._timeout):
            result = run_project_tests("npm", project_dir=str(tmp_project))

        assert not result.passed
        assert CHECK_TIMEOUT_HINT in capsys.readouterr().err

    def test_sync_workspace_timeout_prints_hint(self, tmp_project, capsys):
        """sync_workspace timeout message includes the remediation hint."""
        (tmp_project / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n"
        )
        with (
            patch("rlsbl.testing.require_tool", return_value="/usr/bin/uv"),
            patch("rlsbl.effects.run", side_effect=self._timeout),
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests(
                "pypi", project_dir=str(pkg), workspace_root=str(ws_root)
            )

            assert result.passed
            assert mock_run.call_count == 2
            sync_cmd = mock_run.call_args_list[0][0][0]
            assert sync_cmd == ["uv", "sync", "--all-packages", "--quiet"]
            assert mock_run.call_args_list[0].kwargs["cwd"] == str(ws_root)
            pytest_cmd = mock_run.call_args_list[1][0][0]
            assert pytest_cmd == ["uv", "run", "python", "-P", "-m", "pytest"]
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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "--extra", "test", "python", "-P", "-m", "pytest"]

    def test_standalone_dev_group_default(self, tmp_project):
        """Non-workspace, pytest in [dependency-groups].dev: uv run pytest (default dev)."""
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0", "ruff"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "python", "-P", "-m", "pytest"]

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
            patch("rlsbl.effects.run") as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("pypi", project_dir=str(tmp_project))

            assert result.passed
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["uv", "run", "--group", "testing", "python", "-P", "-m", "pytest"]


# ---------------------------------------------------------------------------
# Regression: PYTHONSAFEPATH (-P) prevents CWD-shadowing at pytest startup
# ---------------------------------------------------------------------------

class TestPythonSafePathShadowing:
    """The pytest invocation uses ``python -P -m pytest`` so a target project's
    flat-layout module shadowing a stdlib name (e.g. a package-root ``html.py``)
    cannot break third-party plugin imports during pytest startup.

    ``python -m`` injects the cwd into sys.path[0]; plugins loaded at startup
    (before any test directory is added) then resolve stdlib names against the
    target project's root. ``-P`` (PYTHONSAFEPATH) suppresses that injection
    while keeping interpreter resolution intact.

    Subprocess-level red-green proof: the same scenario FAILS under
    ``python -m pytest`` and PASSES under ``python -P -m pytest``. This mirrors
    the real failure (pytest-playwright -> slugify -> ``from html.entities ...``)
    without needing those third-party packages installed.

    The probe plugin is loaded via the ``PYTEST_PLUGINS`` env var, which imports
    it during plugin-manager init -- the same early phase as setuptools
    entry-point plugins (pytest-playwright etc.), before pytest inserts the
    rootdir into sys.path. This makes the reproduction independent of the
    ``-p <name>`` vs conftest ordering, which varies across pytest versions.
    """

    def _build_scenario(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        # Flat-layout module shadowing stdlib ``html``; raises on import so a
        # successful stdlib import (the correct behavior) is unambiguous.
        (proj / "html.py").write_text(
            'raise RuntimeError("local html.py shadowed stdlib during plugin load")\n'
        )
        (proj / "test_trivial.py").write_text("def test_ok():\n    assert True\n")
        # The inner pytest runs on rlsbl's own interpreter, so the stricttest
        # plugin is installed there too -- and installing it IS adoption. A
        # throwaway project that declares no safety stance aborts at configure
        # time, which would mask what this scenario actually probes. Declare the
        # most restrictive stance, exactly as a real consumer would.
        (proj / "pytest.ini").write_text(
            "[pytest]\n"
            "stricttest_sockets = deny\n"
            "stricttest_socket_allowlist =\n"
            "stricttest_unix_socket_allowlist =\n"
            "stricttest_loopback = deny\n"
            "stricttest_sandbox_required = false\n"
        )

        plugindir = tmp_path / "plugindir"
        plugindir.mkdir()
        # A third-party-style pytest plugin that imports a stdlib submodule at
        # load time. Lives outside the project dir (on PYTHONPATH), so only the
        # cwd injection from ``python -m`` can cause the stdlib name to resolve
        # to the project's shadowing module.
        (plugindir / "shadowprobe.py").write_text(
            "from html.entities import codepoint2name\n"
            'assert codepoint2name[38] == "amp"\n'
        )
        return proj, plugindir

    def _run(self, proj, plugindir, extra_flags):
        env = dict(os.environ)
        # Set PYTHONPATH to exactly the plugin dir. Never append an inherited
        # value with a trailing separator: an empty PYTHONPATH component means
        # cwd, which would reintroduce the shadow and defeat -P regardless.
        env["PYTHONPATH"] = str(plugindir)
        env["PYTEST_PLUGINS"] = "shadowprobe"
        cmd = [
            sys.executable, *extra_flags, "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
        ]
        return subprocess.run(
            cmd, cwd=str(proj), env=env, capture_output=True, text=True
        )

    def test_without_safepath_fails(self, tmp_path):
        """RED: ``python -m pytest`` breaks -- plugin load hits the shadow."""
        proj, plugindir = self._build_scenario(tmp_path)
        result = self._run(proj, plugindir, [])
        assert result.returncode != 0
        assert "shadowed stdlib" in (result.stdout + result.stderr)

    def test_with_safepath_passes(self, tmp_path):
        """GREEN: ``python -P -m pytest`` succeeds -- cwd not injected, stdlib wins."""
        proj, plugindir = self._build_scenario(tmp_path)
        result = self._run(proj, plugindir, ["-P"])
        assert result.returncode == 0, (result.stdout + result.stderr)
        assert "1 passed" in result.stdout
