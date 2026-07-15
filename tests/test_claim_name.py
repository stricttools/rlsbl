"""Tests for the claim-name command."""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.claim_name import run_cmd


@pytest.fixture
def real_tmpdir(tmp_path):
    """Provide a real temp directory and patch mkdtemp to return it.

    Also patches shutil.rmtree to prevent cleanup so tests can inspect
    written files after run_cmd returns.
    """
    with patch("rlsbl.commands.claim_name.tempfile.mkdtemp", return_value=str(tmp_path)), \
         patch("rlsbl.commands.claim_name.shutil.rmtree"):
        yield tmp_path


class TestClaimName:
    """Tests for claim-name command."""

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_claim_available_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns available, mock npm publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["my-pkg"], {"yes": False})

        mock_check.assert_called_once_with("my-pkg", "npm")
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "publish", "--access", "public"]
        assert call_args[1]["cwd"] == str(real_tmpdir)
        # Verify package.json was written
        pkg_json = json.loads((real_tmpdir / "package.json").read_text())
        assert pkg_json["name"] == "my-pkg"
        assert pkg_json["version"] == "0.0.0"
        assert pkg_json["description"] == "Name reservation"

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_claim_available_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns available, mock uv build+publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "reason": None}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"PYPI_TOKEN": "tok456"}):
            run_cmd("pypi", ["my-pkg"], {"yes": False})

        mock_check.assert_called_once_with("my-pkg", "pypi")
        assert mock_run.call_count == 2
        build_call = mock_run.call_args_list[0]
        assert build_call[0][0] == ["uv", "build"]
        assert build_call[1]["cwd"] == str(real_tmpdir)
        publish_call = mock_run.call_args_list[1]
        assert publish_call[0][0] == ["uv", "publish", "--token", "tok456"]
        assert publish_call[1]["cwd"] == str(real_tmpdir)
        # Verify pyproject.toml was written
        toml_text = (real_tmpdir / "pyproject.toml").read_text()
        assert 'name = "my-pkg"' in toml_text
        assert 'version = "0.0.0"' in toml_text
        # Verify __init__.py was created in underscored package dir
        assert (real_tmpdir / "my_pkg" / "__init__.py").exists()

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_without_yes_exits(self, mock_check):
        """check-name returns taken, no --yes. Exits 1 without publishing."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "reason": "registered"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["taken-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_moniker_shows_conflicting_package(self, mock_check, capsys):
        """A moniker collision surfaces the concrete conflicting package name.

        Regression: the taken branch printed result["reason"] (the internal tag
        "moniker") instead of result["note"], hiding the actual package that
        collided from the user.
        """
        mock_check.return_value = {
            "name": "selfdoc", "registry": "npm", "status": "taken",
            "variants": None, "reason": "moniker",
            "note": "moniker collision with 'self-doc' (npm strips punctuation)",
        }

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["selfdoc"], {"yes": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "self-doc" in err
        # The raw internal tag should not be what is shown to the user.
        assert "appears taken on npm: moniker." not in err

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_with_yes_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns taken, --yes passed. Publish is attempted."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "reason": "registered"}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["taken-pkg"], {"yes": True})

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "publish", "--access", "public"]

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_error_exits(self, mock_check):
        """check-name returns error. Exits 2 without publishing."""
        mock_check.return_value = {"name": "err-pkg", "registry": "npm", "status": "error", "variants": None, "reason": None, "error": "network timeout"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["err-pkg"], {"yes": False})
        assert exc_info.value.code == 2

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_publish_failure_exits(self, mock_check, mock_run, real_tmpdir):
        """npm publish subprocess fails. Error message shown, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}
        mock_run.side_effect = subprocess.CalledProcessError(1, ["npm", "publish"], stderr="403 Forbidden")

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_publish_failure_exits(self, mock_check, mock_run, real_tmpdir):
        """uv publish fails. Error message shown, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "reason": None}
        # uv build succeeds, uv publish fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # uv build
            subprocess.CalledProcessError(1, ["uv", "publish"], stderr="Upload failed"),
        ]

        with patch.dict(os.environ, {"PYPI_TOKEN": "tok456"}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("pypi", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_requires_token(self, mock_check):
        """NPM_TOKEN not set. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}

        env = {k: v for k, v in os.environ.items() if k != "NPM_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_requires_token(self, mock_check):
        """No PYPI_TOKEN or UV_PUBLISH_TOKEN. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "reason": None}

        env = {k: v for k, v in os.environ.items() if k not in ("PYPI_TOKEN", "UV_PUBLISH_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("pypi", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1
