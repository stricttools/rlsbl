"""Tests for monorepo check-names subcommand."""

import json
import os
from io import StringIO
from unittest.mock import patch, call

import pytest

from rlsbl.commands.monorepo import _cmd_check_names, _cmd_init, _cmd_add
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE


def _make_npm_project(base_path, subdir):
    """Create a minimal npm project (package.json) so detect_targets finds it."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + subdir, "version": "0.1.0"}, f)
    return subdir


def _setup_workspace(mock_git_repo, project_names):
    """Initialize workspace and add projects with the given names."""
    _cmd_init({})
    for name in project_names:
        _make_npm_project(mock_git_repo, name)
        _cmd_add([name], {})


class TestMissingTarget:
    def test_missing_target_prints_error_and_exits(self, mock_git_repo, capsys):
        """Missing --target should print error to stderr and exit 1."""
        _setup_workspace(mock_git_repo, ["core"])
        with pytest.raises(SystemExit) as exc_info:
            _cmd_check_names([], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--target is required" in captured.err


class TestBasicCheckNames:
    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_checks_all_projects(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """check-names should check each project name against the target registry."""
        _setup_workspace(mock_git_repo, ["core", "api", "utils"])
        capsys.readouterr()  # clear setup output

        mock_check.side_effect = [
            {"name": "core", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "api", "registry": "npm", "status": "taken",
             "variants": [], "github_count": 5},
            {"name": "utils", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm"})

        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        # header + 3 rows + blank + summary + batch note = 7 lines
        assert len(lines) == 7

        # Header
        assert "Project" in lines[0]
        assert "Checked Name" in lines[0]
        assert "Status" in lines[0]

        # Row content
        assert "core" in lines[1]
        assert "available" in lines[1]
        assert "api" in lines[2]
        assert "taken" in lines[2]
        assert "utils" in lines[3]
        assert "available" in lines[3]

        # Summary and batch note
        assert "Summary: 2 available, 1 taken (3 total)" in captured.out
        assert "Checked with 200ms delay between names." in captured.out

        # Verify _check_single_name was called with correct names
        assert mock_check.call_count == 3
        mock_check.assert_any_call("core", "npm")
        mock_check.assert_any_call("api", "npm")
        mock_check.assert_any_call("utils", "npm")


class TestPrefix:
    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_prefix_transforms_names(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """--prefix should be prepended to each project name."""
        _setup_workspace(mock_git_repo, ["core", "api"])
        capsys.readouterr()

        mock_check.side_effect = [
            {"name": "www-core", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "www-api", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm", "prefix": "www-"})

        # Verify names were prefixed
        mock_check.assert_any_call("www-core", "npm")
        mock_check.assert_any_call("www-api", "npm")

        captured = capsys.readouterr()
        assert "www-core" in captured.out
        assert "www-api" in captured.out


class TestSuffix:
    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_suffix_transforms_names(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """--suffix should be appended to each project name."""
        _setup_workspace(mock_git_repo, ["core", "api"])
        capsys.readouterr()

        mock_check.side_effect = [
            {"name": "core-js", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "api-js", "registry": "npm", "status": "taken",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm", "suffix": "-js"})

        mock_check.assert_any_call("core-js", "npm")
        mock_check.assert_any_call("api-js", "npm")

        captured = capsys.readouterr()
        assert "core-js" in captured.out
        assert "api-js" in captured.out


class TestPrefixAndSuffix:
    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_prefix_and_suffix_combined(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """--prefix and --suffix should both be applied."""
        _setup_workspace(mock_git_repo, ["core"])
        capsys.readouterr()

        mock_check.side_effect = [
            {"name": "@scope/core-lib", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm", "prefix": "@scope/", "suffix": "-lib"})

        mock_check.assert_called_once_with("@scope/core-lib", "npm")

        captured = capsys.readouterr()
        assert "@scope/core-lib" in captured.out


class TestDelay:
    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_delay_applied_between_names(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """Delay should be applied between names, not after the last one."""
        _setup_workspace(mock_git_repo, ["a", "b", "c"])
        capsys.readouterr()
        mock_sleep.reset_mock()

        mock_check.side_effect = [
            {"name": "a", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "b", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "c", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm", "delay": "500"})

        # 3 projects -> 2 delays between them
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.5), call(0.5)])

    @patch("rlsbl.commands.monorepo.commands.time.sleep")
    @patch("rlsbl.commands.check._check_single_name")
    def test_default_delay_200ms(self, mock_check, mock_sleep, mock_git_repo, capsys):
        """Default delay should be 200ms when --delay is not provided."""
        _setup_workspace(mock_git_repo, ["a", "b"])
        capsys.readouterr()
        mock_sleep.reset_mock()

        mock_check.side_effect = [
            {"name": "a", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
            {"name": "b", "registry": "npm", "status": "available",
             "variants": [], "github_count": 0},
        ]

        _cmd_check_names([], {"target": "npm"})

        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_once_with(0.2)
