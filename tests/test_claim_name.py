"""Tests for the claim-name command."""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.claim_name import run_cmd


def _temp_sandbox(monkeypatch, tmp_path):
    """Point the temp-directory root at an empty per-test directory.

    Anything a run creates through ``tempfile`` (directly or via the effect
    seam's live path) lands here, so "created no real temp directory" is an
    exact assertion instead of a guess about a shared /tmp.
    """
    sandbox = tmp_path / "temproot"
    sandbox.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox))
    return sandbox


@pytest.fixture
def real_tmpdir(tmp_path):
    """Provide a real temp directory and patch mkdtemp to return it.

    Also patches shutil.rmtree to prevent cleanup so tests can inspect
    written files after run_cmd returns.
    """
    with patch("rlsbl._effects_direct.mkdtemp", return_value=str(tmp_path)), \
         patch("rlsbl.effects.rmtree"):
        yield tmp_path


class TestClaimName:
    """Tests for claim-name command."""

    @patch("rlsbl.effects.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_claim_available_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns available, mock npm publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["my-pkg"], {"force-publish": True})

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

    @patch("rlsbl.effects.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_claim_available_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns available, mock uv build+publish succeeds."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "reason": None}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"PYPI_TOKEN": "tok456"}):
            run_cmd("pypi", ["my-pkg"], {"force-publish": True})

        mock_check.assert_called_once_with("my-pkg", "pypi")
        assert mock_run.call_count == 2
        build_call = mock_run.call_args_list[0]
        assert build_call[0][0] == ["uv", "build"]
        assert build_call[1]["cwd"] == str(real_tmpdir)
        publish_call = mock_run.call_args_list[1]
        # The token rides the environment, never argv: a process listing and
        # the dry-run would-do log both see the command without the secret.
        assert publish_call[0][0] == ["uv", "publish"]
        assert publish_call[1]["env"]["UV_PUBLISH_TOKEN"] == "tok456"
        assert publish_call[1]["cwd"] == str(real_tmpdir)
        # Verify pyproject.toml was written
        toml_text = (real_tmpdir / "pyproject.toml").read_text()
        assert 'name = "my-pkg"' in toml_text
        assert 'version = "0.0.0"' in toml_text
        # Verify __init__.py was created in underscored package dir
        assert (real_tmpdir / "my_pkg" / "__init__.py").exists()

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_without_force_publish_exits(self, mock_check):
        """check-name returns taken, no --force-publish. Exits 1 without publishing."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "reason": "registered"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["taken-pkg"], {"force-publish": False})
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
            run_cmd("npm", ["selfdoc"], {"force-publish": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "self-doc" in err
        # The raw internal tag should not be what is shown to the user.
        assert "appears taken on npm: moniker." not in err

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_moniker_shows_all_conflicting_packages(self, mock_check, capsys):
        """A multi-conflict moniker collision surfaces every colliding package + the rule.

        The taken branch prefers result["note"], which now enumerates the full
        conflict list rather than only the first package.
        """
        rule = "npm strips dashes, dots, and underscores: these share one moniker"
        mock_check.return_value = {
            "name": "foobar", "registry": "npm", "status": "taken",
            "variants": None, "reason": "moniker",
            "conflicts": ["foo-bar", "foo.bar"],
            "conflict_rule": rule,
            "note": f"moniker collision with 'foo-bar', 'foo.bar' — {rule}",
        }

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["foobar"], {"force-publish": False})
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "foo-bar" in err
        assert "foo.bar" in err
        assert rule in err

    @patch("rlsbl.effects.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_taken_with_force_publish_publishes(self, mock_check, mock_run, real_tmpdir):
        """check-name returns taken, --force-publish passed. Publish is attempted."""
        mock_check.return_value = {"name": "taken-pkg", "registry": "npm", "status": "taken", "variants": None, "reason": "registered"}
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["taken-pkg"], {"force-publish": True})

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "publish", "--access", "public"]

    @patch("rlsbl.commands.check._check_single_name")
    def test_claim_error_exits(self, mock_check):
        """check-name returns error. Exits 2 without publishing."""
        mock_check.return_value = {"name": "err-pkg", "registry": "npm", "status": "error", "variants": None, "reason": None, "error": "network timeout"}

        with pytest.raises(SystemExit) as exc_info:
            run_cmd("npm", ["err-pkg"], {"force-publish": False})
        assert exc_info.value.code == 2

    @patch("rlsbl.effects.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_publish_failure_exits(self, mock_check, mock_run, real_tmpdir):
        """npm publish subprocess fails. Error message shown, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}
        mock_run.side_effect = subprocess.CalledProcessError(1, ["npm", "publish"], stderr="403 Forbidden")

        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"force-publish": True})
        assert exc_info.value.code == 1

    @patch("rlsbl.effects.run")
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
                run_cmd("pypi", ["my-pkg"], {"force-publish": True})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_requires_token(self, mock_check):
        """NPM_TOKEN not set. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "npm", "status": "available", "variants": None, "reason": None}

        env = {k: v for k, v in os.environ.items() if k != "NPM_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("npm", ["my-pkg"], {"force-publish": False})
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_requires_token(self, mock_check):
        """No PYPI_TOKEN or UV_PUBLISH_TOKEN. Error message, exits 1."""
        mock_check.return_value = {"name": "my-pkg", "registry": "pypi", "status": "available", "variants": None, "reason": None}

        env = {k: v for k, v in os.environ.items() if k not in ("PYPI_TOKEN", "UV_PUBLISH_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd("pypi", ["my-pkg"], {"force-publish": True})
        assert exc_info.value.code == 1


class TestClaimNamePreview:
    """`--dry-run` publishes nothing and RECORDS what it would publish.

    This command is the reason the chokepoint exists: a hand-rolled dry-run
    branch once sat above the publish and a later edit walked around it, so a
    "dry run" claimed a name for real.  There is no branch to walk around any
    more -- the writes and the publish go through the effect chokepoint, and
    under --dry-run the framework records them instead of performing them.

    A preview also creates NOTHING on disk: the staging directory the claim
    is assembled in is a recorded stand-in, so ``tempfile.tempdir`` (pointed
    at a per-test sandbox below) stays empty.  The command used to call
    ``tempfile.mkdtemp`` directly, which creates its directory in every mode,
    and the matching ``effects.rmtree`` was recorded rather than performed --
    so every "dry run" left a real directory behind.
    """

    @patch("rlsbl.commands.check._check_single_name")
    def test_npm_preview_records_the_publish_and_runs_nothing(
        self, mock_check, monkeypatch, tmp_path,
    ):
        mock_check.return_value = {
            "name": "my-pkg", "registry": "npm", "status": "available",
            "variants": None, "reason": None,
        }
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NPM_TOKEN", "tok123")
        temp_sandbox = _temp_sandbox(monkeypatch, tmp_path)
        ran = []
        monkeypatch.setattr(
            "rlsbl._effects_direct.run",
            lambda *a, **k: ran.append(a) or MagicMock(returncode=0),
        )

        import rlsbl
        rlsbl._variadic_args = ["my-pkg"]
        try:
            result = rlsbl.app.test(
                ["--dry-run", "claim-name", "--target", "npm"]
            )
        finally:
            rlsbl._variadic_args = []

        assert result.exit_code == 0, result.stderr
        assert ran == [], "a preview must not run a publish"
        assert "write: " in result.stdout and "package.json" in result.stdout
        assert "run: npm publish --access public" in result.stdout, result.stdout
        assert list(temp_sandbox.iterdir()) == [], (
            "a preview created a real staging directory"
        )

    @patch("rlsbl.commands.check._check_single_name")
    def test_pypi_preview_never_renders_the_token(
        self, mock_check, monkeypatch, tmp_path,
    ):
        """The would-do log is printed output: a secret in argv would leak."""
        mock_check.return_value = {
            "name": "my-pkg", "registry": "pypi", "status": "available",
            "variants": None, "reason": None,
        }
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PYPI_TOKEN", "s3cr3t-token-value")
        temp_sandbox = _temp_sandbox(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "rlsbl._effects_direct.run",
            lambda *a, **k: pytest.fail("a preview must not run a publish"),
        )

        import rlsbl
        rlsbl._variadic_args = ["my-pkg"]
        try:
            result = rlsbl.app.test(
                ["--dry-run", "claim-name", "--target", "pypi"]
            )
        finally:
            rlsbl._variadic_args = []

        assert result.exit_code == 0, result.stderr
        assert "run: uv publish" in result.stdout, result.stdout
        assert "s3cr3t-token-value" not in result.stdout
        assert "s3cr3t-token-value" not in result.stderr
        assert list(temp_sandbox.iterdir()) == [], (
            "a preview created a real staging directory"
        )


class TestClaimNameConfirmation:
    """The publish confirmation is the framework's, not this command's.

    `claim-name` declares itself `consequential`, so strictcli prompts before
    dispatch and `--approve-consequential` skips it.  The command's own prompt
    (and its own non-TTY error) is deleted: two confirmations for one decision,
    worded differently, is how a user learns to answer without reading.
    """

    @patch("builtins.input")
    @patch("rlsbl._effects_direct.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_command_no_longer_prompts_itself(
        self, mock_check, mock_run, mock_input, real_tmpdir,
    ):
        mock_check.return_value = {
            "name": "my-pkg", "registry": "npm", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)
        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["my-pkg"], {"force-publish": False})
        mock_input.assert_not_called()
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["npm", "publish", "--access", "public"]

    @patch("builtins.input")
    @patch("rlsbl._effects_direct.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_yes_publishes(self, mock_check, mock_run, mock_input, real_tmpdir):
        mock_check.return_value = {
            "name": "my-pkg", "registry": "npm", "status": "available",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)
        with patch.dict(os.environ, {"NPM_TOKEN": "tok123"}):
            run_cmd("npm", ["my-pkg"], {"force-publish": True})
        mock_input.assert_not_called()
        mock_run.assert_called_once()
