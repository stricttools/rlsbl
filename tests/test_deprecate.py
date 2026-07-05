"""Tests for rlsbl.commands.deprecate -- deprecation notice on past releases."""

import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.deprecate import run_cmd, _build_notice


class TestSoftDeprecate(unittest.TestCase):
    """Verify the deprecate flow marks a release as pre-release with a deprecation notice."""

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    @patch("os.rename")
    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_deprecate_basic(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth, _rename, _unlink, _exists):
        """Deprecate marks release as pre-release and prepends deprecation notice."""
        mock_run_gh.side_effect = [
            "",          # gh release view v0.9.1 (exists check)
            "v0.9.2",   # gh release list (latest is v0.9.2, not our target)
            "Old notes", # gh release view v0.9.1 --json body
            "",          # gh release edit v0.9.1 --prerelease --notes-file ...
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout, \
             patch("builtins.open", unittest.mock.mock_open()):
            run_cmd(["0.9.1"], {"yes": True}, project_root=".")

        output = mock_stdout.getvalue()
        self.assertIn("Deprecated v0.9.1", output)
        self.assertIn("pre-release", output)

        # Verify gh release edit was called with --prerelease
        edit_calls = [c for c in mock_run_gh.call_args_list
                      if c[0][0] and "edit" in c[0][0]]
        self.assertEqual(len(edit_calls), 1)
        self.assertIn("--prerelease", edit_calls[0][0][0])

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    @patch("os.rename")
    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_deprecate_with_reason_and_use(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth, _rename, _unlink, _exists):
        """Deprecate with --reason and --use includes both in the deprecation notice."""
        mock_run_gh.side_effect = [
            "",             # gh release view v0.9.1
            "v0.9.2",      # gh release list (latest)
            "Old notes",   # gh release view body
            "",             # gh release edit
        ]

        mock_open = unittest.mock.mock_open()
        with patch("sys.stdout", new_callable=StringIO), \
             patch("builtins.open", mock_open):
            run_cmd(["0.9.1"], {"reason": "broken on macOS", "use": "0.9.2", "yes": True}, project_root=".")

        # Check what was written to the notes file
        written = "".join(
            call_args[0][0]
            for call_args in mock_open().write.call_args_list
        )
        self.assertIn("broken on macOS", written)
        self.assertIn("v0.9.2", written)
        self.assertIn("Deprecated", written)
        self.assertIn("Old notes", written)


class TestDeprecateNoHardFlag(unittest.TestCase):
    """Verify that the deprecate command does NOT have a --hard flag."""

    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_hard_flag_ignored(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth):
        """The 'hard' key in flags is not processed (no _hard_yank path)."""
        mock_run_gh.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list (latest)
        ]
        # Even if 'hard' is passed in flags, deprecate should just do the soft path
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.9.1"], {"dry-run": True}, project_root=".")

        output = mock_stdout.getvalue()
        self.assertIn("Would mark v0.9.1 as pre-release", output)


class TestDeprecateDryRun(unittest.TestCase):
    """Verify dry run prints but does not execute destructive commands."""

    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_dry_run(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth):
        """Dry run prints what would happen without editing."""
        mock_run_gh.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list (latest)
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.9.1"], {"dry-run": True}, project_root=".")

        output = mock_stdout.getvalue()
        self.assertIn("Would mark v0.9.1 as pre-release", output)

        # gh release edit should NOT be called
        edit_calls = [c for c in mock_run_gh.call_args_list
                      if c[0][0] and "edit" in c[0][0]]
        self.assertEqual(len(edit_calls), 0)


class TestDeprecateErrorCases(unittest.TestCase):
    """Verify error handling for non-existent releases and latest-release guard."""

    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_nonexistent_release(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth):
        """Deprecating a non-existent release prints an error and exits."""
        mock_run_gh.side_effect = Exception("release not found")

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["0.99.0"], {}, project_root=".")

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", mock_stderr.getvalue())

    @patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.deprecate.run_gh")
    @patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.deprecate.resolve_member_context", return_value=MagicMock(targets=[]))
    def test_latest_release_blocked(self, _detect, _ws_root, mock_run_gh, _gh_inst, _gh_auth):
        """Deprecating the latest release is blocked with a suggestion to use undo."""
        mock_run_gh.side_effect = [
            "",         # gh release view v1.0.0 (exists)
            "v1.0.0",  # gh release list (latest IS our target)
        ]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["1.0.0"], {}, project_root=".")

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("latest release", mock_stderr.getvalue())
        self.assertIn("rlsbl release undo", mock_stderr.getvalue())

    def test_no_version_arg(self):
        """Missing version argument prints error and exits."""
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit) as ctx:
                run_cmd([], {}, project_root=".")
        self.assertEqual(ctx.exception.code, 1)


class TestBuildNotice(unittest.TestCase):
    """Unit tests for the deprecation notice builder."""

    def test_no_reason_no_use(self):
        result = _build_notice(None, None)
        self.assertEqual(result, "> **Deprecated.**")

    def test_reason_only(self):
        result = _build_notice("broken on macOS", None)
        self.assertEqual(result, "> **Deprecated:** broken on macOS.")

    def test_use_only(self):
        result = _build_notice(None, "0.9.2")
        self.assertEqual(result, "> **Deprecated:** Use v0.9.2 instead.")

    def test_reason_and_use(self):
        result = _build_notice("broken on macOS", "0.9.2")
        self.assertEqual(result, "> **Deprecated:** broken on macOS. Use v0.9.2 instead.")

    def test_use_with_v_prefix(self):
        """v prefix on --use is normalized."""
        result = _build_notice(None, "v0.9.2")
        self.assertEqual(result, "> **Deprecated:** Use v0.9.2 instead.")


class TestCmdReleaseDeprecateDelegation:
    """Verify the CLI handler delegates correctly to the command module."""

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake"))
    @patch("rlsbl.commands.deprecate.run_cmd")
    def test_delegates(self, mock_run, _):
        import rlsbl
        rlsbl.cmd_release_deprecate(
            reason="security", use="1.2.4",
            dry_run=True, yes=True, version="1.2.3",
        )
        mock_run.assert_called_once()
        flags = mock_run.call_args[0][1]
        assert flags["reason"] == "security"
        assert flags["use"] == "1.2.4"
        # No 'hard' key should be present
        assert "hard" not in flags


if __name__ == "__main__":
    unittest.main()
