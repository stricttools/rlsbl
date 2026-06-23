"""Tests for github target support in check-name command."""

import sys
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest

from conftest import FakeResponse
from rlsbl.commands.check import (
    _check_single_name,
    _format_single_result,
    _format_table_row,
    check_github_availability,
    run_cmd,
)


class TestGithubValidTarget:
    """--target github is accepted as a valid target."""

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_github_target_accepted(self, mock_run_cmd):
        """Passing --target github should call run_cmd with 'github'."""
        from rlsbl import cmd_check_name

        cmd_check_name(target=["github"], delay="200")
        assert mock_run_cmd.call_count == 1
        assert mock_run_cmd.call_args_list[0][0][0] == "github"

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_github_with_other_targets(self, mock_run_cmd):
        """--target npm --target github should call run_cmd for both."""
        from rlsbl import cmd_check_name

        cmd_check_name(target=["npm", "github"], delay="200")
        assert mock_run_cmd.call_count == 2
        targets_called = [c[0][0] for c in mock_run_cmd.call_args_list]
        assert "npm" in targets_called
        assert "github" in targets_called


class TestNpmDoesNotCallGithub:
    """--target npm no longer calls check_github_availability."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    @patch("rlsbl.commands.check._check_variants", return_value=[])
    @patch("rlsbl.commands.check._search_npm_similar", return_value=[])
    def test_npm_no_github_call(self, mock_search, mock_variants, mock_npm, mock_gh):
        mock_npm.return_value = {"status": "available"}
        result = _check_single_name("testpkg", "npm")
        mock_gh.assert_not_called()
        assert "github_count" not in result

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_pypi_availability")
    def test_pypi_no_github_call(self, mock_pypi, mock_gh):
        mock_pypi.return_value = {"status": "taken"}
        result = _check_single_name("testpkg", "pypi")
        mock_gh.assert_not_called()
        assert "github_count" not in result

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_go_availability")
    def test_go_no_github_call(self, mock_go, mock_gh):
        mock_go.return_value = {"status": "not_found"}
        result = _check_single_name("testpkg", "go")
        mock_gh.assert_not_called()
        assert "github_count" not in result


class TestGithubCheckSingleName:
    """--target github returns repo count information via _check_single_name."""

    @patch("rlsbl.commands.check.check_github_availability")
    def test_github_available(self, mock_gh):
        mock_gh.return_value = {"status": "available", "count": 0}
        result = _check_single_name("unique-name-xyz", "github")
        assert result["status"] == "available"
        assert result["github_count"] == 0
        assert result["registry"] == "github"

    @patch("rlsbl.commands.check.check_github_availability")
    def test_github_exists_with_count(self, mock_gh):
        mock_gh.return_value = {"status": "exists", "count": 42, "note": "42 repos with this name on GitHub"}
        result = _check_single_name("react", "github")
        assert result["status"] == "exists"
        assert result["github_count"] == 42
        assert result["note"] == "42 repos with this name on GitHub"

    @patch("rlsbl.commands.check.check_github_availability")
    def test_github_error(self, mock_gh):
        mock_gh.return_value = {"status": "error", "message": "API rate limited"}
        result = _check_single_name("anything", "github")
        assert result["status"] == "error"
        assert result["error"] == "API rate limited"
        assert "github_count" not in result


class TestGithubSingleNameFormat:
    """_format_single_result with registry == 'github' prints correctly."""

    def test_format_github_no_repos(self, capsys):
        result = {
            "name": "unique-name",
            "registry": "github",
            "status": "available",
            "variants": None,
            "reason": None,
            "github_count": 0,
        }
        exit_code = _format_single_result(result)
        captured = capsys.readouterr()
        assert 'Checking GitHub for "unique-name"...' in captured.out
        assert 'No GitHub repos named "unique-name".' in captured.out
        assert "Checked: GitHub" in captured.out
        # Should NOT have the informational (i) block (that's for non-github registries)
        assert "(i)" not in captured.out
        assert exit_code == 0

    def test_format_github_with_repos(self, capsys):
        result = {
            "name": "react",
            "registry": "github",
            "status": "exists",
            "variants": None,
            "reason": None,
            "github_count": 42,
        }
        exit_code = _format_single_result(result)
        captured = capsys.readouterr()
        assert '42 GitHub repo(s) named "react".' in captured.out
        assert "org-scoped" in captured.out
        # Should NOT have the informational (i) block
        assert "(i)" not in captured.out
        assert exit_code == 1  # "exists" maps to exit code 1

    def test_format_github_error(self, capsys):
        result = {
            "name": "anything",
            "registry": "github",
            "status": "error",
            "variants": None,
            "reason": None,
            "error": "API rate limited",
        }
        exit_code = _format_single_result(result)
        captured = capsys.readouterr()
        assert "Error checking GitHub" in captured.err
        assert exit_code == 2

    def test_no_double_output_github(self, capsys):
        """When registry is github, the informational block must not print."""
        result = {
            "name": "test",
            "registry": "github",
            "status": "exists",
            "variants": None,
            "reason": None,
            "github_count": 5,
        }
        _format_single_result(result)
        captured = capsys.readouterr()
        # Count occurrences of the name in context of GitHub output
        # Should appear in "Checking GitHub" and the count line, but NOT in an "(i)" block
        assert captured.out.count("(i)") == 0


class TestGithubTableRow:
    """_format_table_row handles github results."""

    def test_table_row_no_repos(self):
        result = {
            "name": "unique-name",
            "registry": "github",
            "status": "available",
            "github_count": 0,
        }
        row = _format_table_row(result)
        assert row["name"] == "unique-name"
        assert row["status"] == "no repos"

    def test_table_row_with_repos(self):
        result = {
            "name": "react",
            "registry": "github",
            "status": "exists",
            "github_count": 42,
        }
        row = _format_table_row(result)
        assert row["name"] == "react"
        assert row["status"] == "42 repos"

    def test_table_row_error(self):
        result = {
            "name": "anything",
            "registry": "github",
            "status": "error",
        }
        row = _format_table_row(result)
        assert row["status"] == "error"


class TestGithubMultiNameMode:
    """Multi-name mode works with github target."""

    @patch("rlsbl.commands.check.check_github_availability")
    def test_multi_name_github(self, mock_gh, capsys):
        mock_gh.side_effect = [
            {"status": "available", "count": 0},
            {"status": "exists", "count": 10, "note": "10 repos"},
        ]
        with pytest.raises(SystemExit) as exc_info:
            run_cmd("github", ["unique-name", "common-name"], {"delay": "0"})
        captured = capsys.readouterr()
        assert "unique-name" in captured.out
        assert "common-name" in captured.out
        assert "no repos" in captured.out
        assert "10 repos" in captured.out
        # exit code 1 because at least one "exists"
        assert exc_info.value.code == 1


class TestClaimNameNoGithub:
    """claim_name.py still works and doesn't call GitHub API."""

    @patch("rlsbl.commands.check.check_github_availability")
    @patch("rlsbl.commands.check.check_npm_availability")
    @patch("rlsbl.commands.check._check_variants", return_value=[])
    @patch("rlsbl.commands.check._search_npm_similar", return_value=[])
    def test_claim_name_npm_no_github(self, mock_search, mock_variants, mock_npm, mock_gh):
        """claim_name imports _check_single_name which should not call GitHub."""
        mock_npm.return_value = {"status": "available"}
        from rlsbl.commands.claim_name import run_cmd as claim_run_cmd

        # Simulate the check that claim_name does internally
        result = _check_single_name("test-pkg", "npm")
        mock_gh.assert_not_called()

    def test_claim_name_rejects_github_target(self):
        """claim_name only supports npm and pypi, not github."""
        from rlsbl.commands.claim_name import run_cmd as claim_run_cmd

        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("github", ["test-pkg"], {"yes": False})
        assert exc_info.value.code == 1


class TestCheckedSummaryNoGithubLeak:
    """The 'Checked:' summary line should not include 'GitHub repos' for non-github targets."""

    def test_npm_summary_no_github(self, capsys):
        result = {
            "name": "test",
            "registry": "npm",
            "status": "available",
            "variants": None,
            "reason": None,
        }
        _format_single_result(result)
        captured = capsys.readouterr()
        assert "GitHub repos" not in captured.out
        assert "Checked: npm" in captured.out

    def test_github_summary_includes_github(self, capsys):
        result = {
            "name": "test",
            "registry": "github",
            "status": "available",
            "variants": None,
            "reason": None,
            "github_count": 0,
        }
        _format_single_result(result)
        captured = capsys.readouterr()
        assert "Checked: GitHub, GitHub repos" in captured.out
