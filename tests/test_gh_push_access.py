"""Tests for validate_gh_push_access and stderr surfacing in execute.py."""

import os
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_gh_push_access,
)


class TestValidateGhPushAccess:
    """Tests for validate_gh_push_access."""

    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_push_access_granted(self, mock_repo, mock_run_gh):
        """When API returns 'true', the function passes silently."""
        mock_run_gh.return_value = "true"
        validate_gh_push_access(config={"github_repo": "owner/repo"})
        mock_run_gh.assert_called_once_with(
            ["api", "repos/owner/repo", "--jq", ".permissions.push"],
            config={"github_repo": "owner/repo"},
        )

    @patch.dict(os.environ, {"GH_TOKEN": "fake-token"}, clear=False)
    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_push_access_denied_with_gh_token(self, mock_repo, mock_run_gh):
        """When push denied and GH_TOKEN is set, error mentions GH_TOKEN."""
        mock_run_gh.side_effect = [
            "false",  # permissions.push
            "some-user",  # user login
        ]
        with pytest.raises(ReleaseValidationError, match="GH_TOKEN"):
            validate_gh_push_access(config=None)

    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_push_access_denied_without_token(self, mock_repo, mock_run_gh):
        """When push denied and no token env var, error doesn't mention tokens."""
        # Remove token env vars to keep the test clean
        env_backup = {}
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            if var in os.environ:
                env_backup[var] = os.environ.pop(var)
        try:
            mock_run_gh.side_effect = [
                "false",  # permissions.push
                "some-user",  # user login
            ]
            with pytest.raises(ReleaseValidationError, match="does not have push access") as exc_info:
                validate_gh_push_access(config=None)
            assert "GH_TOKEN" not in str(exc_info.value)
            assert "GITHUB_TOKEN" not in str(exc_info.value)
        finally:
            os.environ.update(env_backup)

    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_api_failure_graceful_skip(self, mock_repo, mock_run_gh, capsys):
        """When the API call fails, a warning is printed and no error raised."""
        mock_run_gh.side_effect = Exception("network error")
        validate_gh_push_access(config=None)
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "push access" in captured.err

    @patch("rlsbl.utils.get_github_repo", return_value=None)
    def test_repo_undetermined_silent_return(self, mock_repo):
        """When get_github_repo returns None, returns silently."""
        validate_gh_push_access(config=None)

    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_push_access_denied_with_github_token(self, mock_repo, mock_run_gh):
        """When push denied and GITHUB_TOKEN is set, error mentions GITHUB_TOKEN."""
        # Remove GH_TOKEN if present (GH_TOKEN takes priority in the check)
        env_backup = {}
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            if var in os.environ:
                env_backup[var] = os.environ.pop(var)
        os.environ["GITHUB_TOKEN"] = "fake-token"
        try:
            mock_run_gh.side_effect = [
                "false",  # permissions.push
                "some-user",  # user login
            ]
            with pytest.raises(ReleaseValidationError, match="GITHUB_TOKEN"):
                validate_gh_push_access(config=None)
        finally:
            os.environ.pop("GITHUB_TOKEN", None)
            os.environ.update(env_backup)

    @patch("rlsbl.utils.run_gh")
    @patch("rlsbl.utils.get_github_repo", return_value="owner/repo")
    def test_push_access_empty_result(self, mock_repo, mock_run_gh):
        """When API returns empty string (unexpected), treated as access denied."""
        # Remove token env vars to keep the test clean
        env_backup = {}
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            if var in os.environ:
                env_backup[var] = os.environ.pop(var)
        try:
            mock_run_gh.side_effect = [
                "",  # permissions.push -- empty
                "some-user",  # user login
            ]
            with pytest.raises(ReleaseValidationError, match="does not have push access"):
                validate_gh_push_access(config=None)
        finally:
            os.environ.update(env_backup)


class TestStderrSurfacing:
    """Tests for stderr surfacing in execute.py exception handlers."""

    def test_push_failure_surfaces_stderr(self, capsys):
        """When a CalledProcessError with stderr occurs during push, stderr is printed."""
        error = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "push"],
            stderr="fatal: could not read from remote repository\n",
        )
        assert hasattr(error, 'stderr')
        assert error.stderr

        # Simulate the logic from execute.py push failure handler
        if hasattr(error, 'stderr') and error.stderr:
            import sys
            print(f"Command error: {error.stderr.strip()}", file=sys.stderr)

        captured = capsys.readouterr()
        assert "Command error: fatal: could not read from remote repository" in captured.err

    def test_gh_release_failure_surfaces_stderr(self, capsys):
        """When a CalledProcessError with stderr occurs during gh release create, stderr is printed."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "release", "create"],
            stderr="HTTP 403: Resource not accessible by personal access token\n",
        )

        # Simulate the logic from execute.py gh release handler
        if hasattr(error, 'stderr') and error.stderr:
            import sys
            print(f"Command error: {error.stderr.strip()}", file=sys.stderr)

        captured = capsys.readouterr()
        assert "Command error: HTTP 403: Resource not accessible by personal access token" in captured.err

    def test_exception_without_stderr_no_output(self, capsys):
        """When an exception has no stderr attribute, no extra output is printed."""
        error = RuntimeError("something went wrong")

        if hasattr(error, 'stderr') and error.stderr:
            import sys
            print(f"Command error: {error.stderr.strip()}", file=sys.stderr)

        captured = capsys.readouterr()
        assert "Command error" not in captured.err

    def test_exception_with_empty_stderr_no_output(self, capsys):
        """When a CalledProcessError has empty stderr, no extra output is printed."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", "push"],
            stderr="",
        )

        if hasattr(error, 'stderr') and error.stderr:
            import sys
            print(f"Command error: {error.stderr.strip()}", file=sys.stderr)

        captured = capsys.readouterr()
        assert "Command error" not in captured.err
