"""Tests for the claim-name command."""

import json
import os
import subprocess
import sys
from unittest.mock import patch, MagicMock, call

import pytest

from rlsbl.commands.claim_name import run_cmd


class TestClaimName:
    """Tests for claim-name command."""

    @patch("rlsbl.commands.claim_name.shutil.rmtree")
    @patch("rlsbl.commands.claim_name.tempfile.mkdtemp")
    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_claim_available_publishes(self, mock_check, mock_run, mock_mkdtemp, mock_rmtree):
        """check-name returns available, mock npm publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "github_count": None, "reason": None}
        mock_mkdtemp.return_value = "/tmp/fakedir"
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["my-pkg"], {"yes": False})

        mock_check.assert_called_once_with("my-pkg", "npm")
        # npm publish was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "publish", "--access", "public"]
        assert call_args[1]["cwd"] == "/tmp/fakedir"
        mock_rmtree.assert_called_once_with("/tmp/fakedir")

    @patch("rlsbl.commands.claim_name.shutil.rmtree")
    @patch("rlsbl.commands.claim_name.tempfile.mkdtemp")
    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_claim_available_publishes(self, mock_check, mock_run, mock_mkdtemp, mock_rmtree):
        """check-name returns available, mock uv build+publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "github_count": None, "reason": None}
        mock_mkdtemp.return_value = "/tmp/fakedir"
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"PYPI_TOKEN": "tok456"}):
            run_cmd("pypi", ["my-pkg"], {"yes": False})

        mock_check.assert_called_once_with("my-pkg", "pypi")
        # uv build then uv publish
        assert mock_run.call_count == 2
        build_call = mock_run.call_args_list[0]
        assert build_call[0][0] == ["uv", "build"]
        assert build_call[1]["cwd"] == "/tmp/fakedir"
        publish_call = mock_run.call_args_list[1]
        assert publish_call[0][0] == ["uv", "publish", "--token", "tok456"]
        assert publish_call[1]["cwd"] == "/tmp/fakedir"
        mock_rmtree.assert_called_once_with("/tmp/fakedir")

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_without_yes_exits(self, mock_check):
        """check-name returns taken, no --yes. Exits 1 without publishing."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "github_count": None, "reason": "registered"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["taken-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.claim_name.shutil.rmtree")
    @patch("rlsbl.commands.claim_name.tempfile.mkdtemp")
    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_with_yes_publishes(self, mock_check, mock_run, mock_mkdtemp, mock_rmtree):
        """check-name returns taken, --yes passed. Publish is attempted."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "github_count": None, "reason": "registered"}
        mock_mkdtemp.return_value = "/tmp/fakedir"
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["taken-pkg"], {"yes": True})

        # Should still call npm publish despite taken status
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "publish", "--access", "public"]

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_error_exits(self, mock_check):
        """check-name returns error. Exits 2 without publishing."""
        mock_check.return_value = {"name": "err-pkg", "registry": "npm", "status": "error", "variants": None, "github_count": None, "reason": None, "error": "network timeout"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["err-pkg"], {"yes": False})
        assert exc_info.value.code == 2

    @patch("rlsbl.commands.claim_name.shutil.rmtree")
    @patch("rlsbl.commands.claim_name.tempfile.mkdtemp")
    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_publish_failure_exits(self, mock_check, mock_run, mock_mkdtemp, mock_rmtree):
        """npm publish subprocess fails. Error message shown, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "github_count": None, "reason": None}
        mock_mkdtemp.return_value = "/tmp/fakedir"
        mock_run.side_effect = subprocess.CalledProcessError(1, ["npm", "publish"], stderr="403 Forbidden")

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1
        mock_rmtree.assert_called_once_with("/tmp/fakedir")

    @patch("rlsbl.commands.claim_name.shutil.rmtree")
    @patch("rlsbl.commands.claim_name.tempfile.mkdtemp")
    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_publish_failure_exits(self, mock_check, mock_run, mock_mkdtemp, mock_rmtree):
        """uv publish fails. Error message shown, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "github_count": None, "reason": None}
        mock_mkdtemp.return_value = "/tmp/fakedir"
        # uv build succeeds, uv publish fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # uv build
            subprocess.CalledProcessError(1, ["uv", "publish"], stderr="Upload failed"),
        ]

        with patch.dict(os.environ, {"PYPI_TOKEN": "tok456"}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("pypi", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1
        mock_rmtree.assert_called_once_with("/tmp/fakedir")

    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_requires_token(self, mock_check):
        """NPM_TOKEN not set. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "github_count": None, "reason": None}

        # Ensure NPM_TOKEN is NOT in the environment
        env = {k: v for k, v in os.environ.items() if k != "NPM_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_requires_token(self, mock_check):
        """No PYPI_TOKEN or UV_PUBLISH_TOKEN. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "github_count": None, "reason": None}

        # Ensure neither token is in the environment
        env = {k: v for k, v in os.environ.items() if k not in ("PYPI_TOKEN", "UV_PUBLISH_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("pypi", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1
