"""Tests for built-in pre-release checks (tests, lint) and the two-hook model."""

import json
import subprocess
from unittest.mock import patch

import pytest

from pathlib import Path

from rlsbl.commands.release import _run_selfdoc_check, _run_selfdoc_gen, HookError
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_npm_project(tmp_path, test_script=None):
    """Create a minimal npm project in tmp_path.

    If test_script is a string, add it as the "test" script in package.json.
    If test_script is None, no scripts section is created.
    """
    pkg = {"name": "test-pkg", "version": "1.0.0"}
    if test_script is not None:
        pkg["scripts"] = {"test": test_script}
    (tmp_path / "package.json").write_text(json.dumps(pkg) + "\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release with improvements.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"]}) + "\n"
    )


def _setup_pypi_project(tmp_path):
    """Create a minimal pypi project in tmp_path with pytest declared."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-pkg"\nversion = "1.0.0"\n\n'
        '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release with improvements.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")


def _setup_go_project(tmp_path):
    """Create a minimal go project in tmp_path."""
    (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.21\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release with improvements.\n"
    )


# ---------------------------------------------------------------------------
# Built-in selfdoc check tests
# ---------------------------------------------------------------------------

class TestSelfdocCheck:
    """Tests for _run_selfdoc_check()."""

    def test_selfdoc_check_runs_when_present(self, tmp_project):
        """When selfdoc.json exists and selfdoc is on PATH, subprocess.run is called."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool") as mock_which,
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            mock_which.return_value = "/usr/bin/selfdoc"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = _run_selfdoc_check({})

            assert result is True
            mock_run.assert_called_once_with(
                ["selfdoc", "check"], cwd=None, check=True
            )

    def test_selfdoc_check_skipped_when_no_config(self, tmp_project):
        """When selfdoc.json does not exist, function returns without running."""
        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            result = _run_selfdoc_check({})

            assert result is True
            mock_run.assert_not_called()

    def test_selfdoc_check_skipped_when_not_installed(self, tmp_project, capsys):
        """When selfdoc.json exists but selfdoc is not on PATH, print note and return."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value=None),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            result = _run_selfdoc_check({})

            assert result is True
            mock_run.assert_not_called()
            captured = capsys.readouterr()
            assert "selfdoc is not installed" in captured.out

    def test_selfdoc_check_failure_propagates(self, tmp_project):
        """When selfdoc check fails, sys.exit(1) is called."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch(
                "rlsbl.commands.release.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["selfdoc", "check"]),
            ),
        ):
            with pytest.raises(HookError, match="selfdoc check failed"):
                _run_selfdoc_check({})

    def test_selfdoc_check_uses_project_dir(self, tmp_project):
        """When project_dir is set, selfdoc.json is checked there and cwd is passed."""
        project_dir = tmp_project / "libs" / "mylib"
        project_dir.mkdir(parents=True)
        (project_dir / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = _run_selfdoc_check({}, project_dir=str(project_dir))

            assert result is True
            mock_run.assert_called_once_with(
                ["selfdoc", "check"], cwd=str(project_dir), check=True
            )

    def test_selfdoc_check_dry_run_skips(self, tmp_project):
        """When dry-run flag is set, selfdoc check is skipped."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            result = _run_selfdoc_check({"dry-run": True})

            assert result is True
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Built-in selfdoc gen tests
# ---------------------------------------------------------------------------

class TestSelfdocGen:
    """Tests for _run_selfdoc_gen()."""

    def test_selfdoc_gen_runs_when_present(self, tmp_project):
        """When selfdoc.json exists and selfdoc is on PATH, subprocess.run is called."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool") as mock_which,
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            mock_which.return_value = "/usr/bin/selfdoc"
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = _run_selfdoc_gen({})

            assert result is True
            mock_run.assert_called_once_with(
                ["selfdoc", "gen", "--no-commit"], cwd=None, check=True
            )

    def test_selfdoc_gen_skipped_when_no_config(self, tmp_project):
        """When selfdoc.json does not exist, function returns without running."""
        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            result = _run_selfdoc_gen({})

            assert result is True
            mock_run.assert_not_called()

    def test_selfdoc_gen_skipped_when_not_installed(self, tmp_project, capsys):
        """When selfdoc.json exists but selfdoc is not on PATH, print note and return."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value=None),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            result = _run_selfdoc_gen({})

            assert result is True
            mock_run.assert_not_called()
            captured = capsys.readouterr()
            assert "selfdoc is not installed" in captured.out

    def test_selfdoc_gen_failure_exits(self, tmp_project):
        """When selfdoc gen fails, sys.exit(1) is called."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch(
                "rlsbl.commands.release.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["selfdoc", "gen", "--no-commit"]),
            ),
        ):
            with pytest.raises(HookError, match="selfdoc gen failed"):
                _run_selfdoc_gen({})

    def test_selfdoc_gen_uses_project_dir(self, tmp_project):
        """When project_dir is set, selfdoc.json is checked there and cwd is passed."""
        project_dir = tmp_project / "libs" / "mylib"
        project_dir.mkdir(parents=True)
        (project_dir / "selfdoc.json").write_text("{}")

        with (
            patch("rlsbl.commands.release.require_tool", return_value="/usr/bin/selfdoc"),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = _run_selfdoc_gen({}, project_dir=str(project_dir))

            assert result is True
            mock_run.assert_called_once_with(
                ["selfdoc", "gen", "--no-commit"], cwd=str(project_dir), check=True
            )

    def test_selfdoc_gen_dry_run_logs(self, tmp_project, capsys):
        """When dry-run flag is set, selfdoc gen is skipped with a log message."""
        (tmp_project / "selfdoc.json").write_text("{}")

        with patch("rlsbl.commands.release.subprocess.run") as mock_run:
            result = _run_selfdoc_gen({"dry-run": True})

            assert result is True
            mock_run.assert_not_called()
            captured = capsys.readouterr()
            assert "Would run: selfdoc gen --no-commit" in captured.out


# ---------------------------------------------------------------------------
# Two-hook model tests
# ---------------------------------------------------------------------------

class TestTwoHookModel:
    """Tests for pre-checks and pre-release hook ordering."""

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_checks_hook_runs_before_preflight(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """pre-checks.sh runs before preflight checks (which are skipped in dry-run)."""
        _setup_npm_project(tmp_project, test_script="jest")
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        marker = tmp_project / "pre-checks-ran"
        (hooks_dir / "pre-checks.sh").write_text(
            f"#!/bin/bash\ntouch {marker}\n"
        )
        (hooks_dir / "pre-checks.sh").chmod(0o755)

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, ctx=ProjectContext(project_root=Path(str(tmp_project)), workspace_root=None, config={"private": False, "pipelines": {}}))

        # The hook should have actually run and created the marker
        assert marker.exists(), "pre-checks.sh should have created the marker file"

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_release_hook_runs_after_preflight(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """pre-release.sh runs after preflight checks."""
        _setup_npm_project(tmp_project, test_script=None)
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        pre_release_marker = tmp_project / "pre-release-ran"
        (hooks_dir / "pre-release.sh").write_text(
            f"#!/bin/bash\ntouch {pre_release_marker}\n"
        )
        (hooks_dir / "pre-release.sh").chmod(0o755)

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        run_cmd(_rc(), {"dry-run": True, "quiet": True, "yes": True}, ctx=ProjectContext(project_root=Path(str(tmp_project)), workspace_root=None, config={"private": False, "pipelines": {}}))

        # pre-release hook still executes in dry-run mode
        assert pre_release_marker.exists(), (
            "pre-release.sh should have run after preflight checks"
        )

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_pre_checks_hook_failure_aborts_before_preflight(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """A failing pre-checks.sh aborts the release before running preflight checks."""
        _setup_npm_project(tmp_project, test_script="jest")
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-checks.sh").write_text("#!/bin/bash\nexit 1\n")
        (hooks_dir / "pre-checks.sh").chmod(0o755)

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        with patch("rlsbl.app.run_checks") as mock_checks:
            from rlsbl.commands.release import run_cmd

            with pytest.raises(SystemExit) as exc_info:
                run_cmd(_rc(), {"quiet": True, "yes": True}, ctx=ProjectContext(project_root=Path(str(tmp_project)), workspace_root=None, config={"private": False, "pipelines": {}}))

            assert exc_info.value.code == 1
            # Preflight checks should NOT have been called
            mock_checks.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: full flow order
# ---------------------------------------------------------------------------

class TestFullFlowOrder:
    """Tests verifying the execution order of all pre-release components."""

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_execution_order(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        _remote_exists,
        tmp_project,
    ):
        """Verify order: pre-checks hook -> preflight checks -> pre-release hook.

        The pre-release hook must contain the scaffold template content so
        that _is_hook_effectively_empty() returns True and preflight checks
        are not skipped by the hooks-override behavior.
        """
        _setup_npm_project(tmp_project, test_script=None)
        hooks_dir = tmp_project / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)

        # Track the order of all execution steps in a single list.
        execution_order = []

        # pre-checks hook: custom script that records its execution.
        (hooks_dir / "pre-checks.sh").write_text(
            "#!/bin/bash\nexit 0\n"
        )
        (hooks_dir / "pre-checks.sh").chmod(0o755)

        # pre-release hook: must match a known scaffold template hash so it
        # is treated as "effectively empty" (preflight checks still run).
        _V1_TEMPLATE = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "# Project-specific pre-release checks.\n"
            "# Built-in checks (tests, lint) run automatically before this hook.\n"
            "# Add custom validation here, e.g.:\n"
            "#   - Check for uncommitted documentation\n"
            "#   - Verify external service connectivity\n"
            "#   - Run integration tests not covered by the test suite\n"
        )
        (hooks_dir / "pre-release.sh").write_text(_V1_TEMPLATE)
        (hooks_dir / "pre-release.sh").chmod(0o755)

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        def tracking_run_checks(ctx, *, tag_expr=None, **kwargs):
            execution_order.append("preflight")
            return ([], 0)

        # Wrap subprocess.run to record hook invocations by name.
        original_subprocess_run = subprocess.run

        def tracking_subprocess_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) >= 2 and "pre-checks.sh" in cmd[1]:
                execution_order.append("pre-checks")
            elif isinstance(cmd, list) and len(cmd) >= 2 and "pre-release.sh" in cmd[1]:
                execution_order.append("pre-release")
            return original_subprocess_run(*args, **kwargs)

        with (
            patch("rlsbl.app.run_checks", side_effect=tracking_run_checks),
            patch("subprocess.run", side_effect=tracking_subprocess_run),
        ):
            from rlsbl.commands.release import run_cmd

            run_cmd(_rc(), {"quiet": True, "yes": True}, ctx=ProjectContext(project_root=Path(str(tmp_project)), workspace_root=None, config={"private": False, "pipelines": {}}))

        # Full execution order: pre-checks -> preflight -> pre-release
        assert execution_order == ["pre-checks", "preflight", "pre-release"]
