"""Tests for crates.io claim-name support."""

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.claim_name import run_cmd, _read_cargo_token


@pytest.fixture
def real_tmpdir(tmp_path):
    """Provide a real temp directory and patch mkdtemp to return it.

    Also patches shutil.rmtree to prevent cleanup so tests can inspect
    written files after run_cmd returns.
    """
    with patch("rlsbl.commands.claim_name.tempfile.mkdtemp", return_value=str(tmp_path)), \
         patch("rlsbl.commands.claim_name.shutil.rmtree"):
        yield tmp_path


class TestReadCargoToken:
    """Tests for reading cargo credentials."""

    def test_reads_token_from_credentials(self, tmp_path):
        creds = tmp_path / "credentials.toml"
        creds.write_text('[registry]\ntoken = "cio_testtoken123"\n')
        with patch("rlsbl.commands.claim_name.os.path.expanduser", return_value=str(creds)):
            token = _read_cargo_token()
        assert token == "cio_testtoken123"

    def test_returns_none_when_missing(self, tmp_path):
        missing = str(tmp_path / "nonexistent.toml")
        with patch("rlsbl.commands.claim_name.os.path.expanduser", return_value=missing):
            token = _read_cargo_token()
        assert token is None

    def test_returns_none_when_no_registry_section(self, tmp_path):
        creds = tmp_path / "credentials.toml"
        creds.write_text('[other]\nkey = "value"\n')
        with patch("rlsbl.commands.claim_name.os.path.expanduser", return_value=str(creds)):
            token = _read_cargo_token()
        assert token is None


class TestClaimNameCrates:
    """Tests for claim-name --target crates."""

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value="cio_tok")
    def test_crates_claim_available_publishes(self, mock_token, mock_check, mock_run, real_tmpdir):
        """Available name + valid token + --yes -> cargo publish succeeds."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)

        run_cmd("crates", ["my-crate"], {"yes": True})

        mock_check.assert_called_once_with("my-crate", "crates")
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["cargo", "publish", "--allow-dirty"]
        assert call_args[1]["cwd"] == str(real_tmpdir)

        # Verify Cargo.toml was written
        cargo_toml = (real_tmpdir / "Cargo.toml").read_text()
        assert 'name = "my-crate"' in cargo_toml
        assert 'version = "0.0.0"' in cargo_toml
        assert 'edition = "2021"' in cargo_toml
        assert 'description = "Name placeholder"' in cargo_toml

        # Verify src/lib.rs was created
        assert (real_tmpdir / "src" / "lib.rs").exists()

    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value=None)
    def test_crates_no_token_exits(self, mock_token, mock_check):
        """No cargo token -> error exit."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd("crates", ["my-crate"], {"yes": True})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_crates_taken_without_yes_exits(self, mock_check):
        """Taken name without --yes -> exit 1."""
        mock_check.return_value = {
            "name": "serde", "registry": "crates", "status": "taken",
            "variants": None, "reason": "registered",
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd("crates", ["serde"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value="cio_tok")
    def test_crates_publish_failure_exits(self, mock_token, mock_check, mock_run, real_tmpdir):
        """cargo publish fails -> exit 1."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["cargo", "publish"], stderr="publish error",
        )

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("crates", ["my-crate"], {"yes": True})
        assert exc_info.value.code == 1

    @patch("builtins.input", return_value="n")
    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value="cio_tok")
    def test_crates_permanence_confirmation_no(self, mock_token, mock_check, mock_input):
        """User answers 'n' to permanence confirmation -> abort."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        with pytest.raises(SystemExit) as exc_info:
            run_cmd("crates", ["my-crate"], {"yes": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("builtins.input", return_value="y")
    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value="cio_tok")
    def test_crates_permanence_confirmation_yes(self, mock_token, mock_check, mock_input, mock_run, real_tmpdir):
        """User answers 'y' to permanence confirmation -> proceeds."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)

        run_cmd("crates", ["my-crate"], {"yes": False})
        mock_run.assert_called_once()

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    @patch("rlsbl.commands.claim_name._read_cargo_token", return_value="cio_tok")
    def test_crates_yes_skips_confirmation(self, mock_token, mock_check, mock_run, real_tmpdir):
        """--yes flag skips the permanence confirmation."""
        mock_check.return_value = {
            "name": "my-crate", "registry": "crates", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)

        # Should not call input() at all
        with patch("builtins.input") as mock_input:
            run_cmd("crates", ["my-crate"], {"yes": True})
            mock_input.assert_not_called()


class TestClaimNameCratesTarget:
    """Tests for crates target acceptance in claim-name CLI."""

    def test_crates_target_accepted_in_cli(self):
        """The crates target is accepted by the claim-name CLI validation."""
        from rlsbl import cmd_claim_name
        # Verify it does not reject 'crates' as unknown
        with patch("rlsbl._variadic_args", ["my-crate"]):
            with patch("rlsbl.commands.claim_name.run_cmd") as mock_run:
                cmd_claim_name(target="crates", yes=True)
                mock_run.assert_called_once_with("crates", ["my-crate"], {"yes": True})
