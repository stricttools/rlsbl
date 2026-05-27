"""Tests for rlsbl.commands.yank — soft yank, hard yank, dry run, and error cases."""

import unittest
from io import StringIO
from unittest.mock import patch, call

from rlsbl.commands.yank import run_cmd, _build_notice


class TestSoftYank(unittest.TestCase):
    """Verify the soft yank flow marks a release as pre-release with a deprecation notice."""

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    @patch("os.rename")
    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_soft_yank_basic(self, mock_run, _gh_inst, _gh_auth, _rename, _unlink, _exists):
        """Soft yank marks release as pre-release and prepends deprecation notice."""
        mock_run.side_effect = [
            "",          # gh release view v0.9.1 (exists check)
            "v0.9.2",   # gh release list (latest is v0.9.2, not our target)
            "Old notes", # gh release view v0.9.1 --json body
            "",          # gh release edit v0.9.1 --prerelease --notes-file ...
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout, \
             patch("builtins.open", unittest.mock.mock_open()):
            run_cmd(["0.9.1"], {"yes": True})

        output = mock_stdout.getvalue()
        self.assertIn("Yanked v0.9.1", output)
        self.assertIn("pre-release", output)

        # Verify gh release edit was called with --prerelease
        edit_calls = [c for c in mock_run.call_args_list
                      if len(c[0]) >= 2 and "edit" in c[0][1]]
        self.assertEqual(len(edit_calls), 1)
        self.assertIn("--prerelease", edit_calls[0][0][1])

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    @patch("os.rename")
    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_soft_yank_with_reason_and_use(self, mock_run, _gh_inst, _gh_auth, _rename, _unlink, _exists):
        """Soft yank with --reason and --use includes both in the deprecation notice."""
        mock_run.side_effect = [
            "",             # gh release view v0.9.1
            "v0.9.2",      # gh release list (latest)
            "Old notes",   # gh release view body
            "",             # gh release edit
        ]

        mock_open = unittest.mock.mock_open()
        with patch("sys.stdout", new_callable=StringIO), \
             patch("builtins.open", mock_open):
            run_cmd(["0.9.1"], {"reason": "broken on macOS", "use": "0.9.2", "yes": True})

        # Check what was written to the notes file
        written = "".join(
            call_args[0][0]
            for call_args in mock_open().write.call_args_list
        )
        self.assertIn("broken on macOS", written)
        self.assertIn("v0.9.2", written)
        self.assertIn("Deprecated", written)
        # Old notes should also be present
        self.assertIn("Old notes", written)


class TestHardYank(unittest.TestCase):
    """Verify the hard yank flow deletes the GitHub Release."""

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_hard_yank(self, mock_run, _gh_inst, _gh_auth):
        """Hard yank deletes the release."""
        mock_run.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list (latest)
            "",         # gh release delete v0.9.1 --yes
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.9.1"], {"hard": True, "yes": True})

        output = mock_stdout.getvalue()
        self.assertIn("Deleted GitHub Release v0.9.1", output)

        # Verify gh release delete was called
        mock_run.assert_any_call("gh", ["release", "delete", "v0.9.1", "--yes"])


class TestDryRun(unittest.TestCase):
    """Verify dry run prints but does not execute destructive commands."""

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_soft_dry_run(self, mock_run, _gh_inst, _gh_auth):
        """Soft dry run prints what would happen without editing."""
        mock_run.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list (latest)
            "Old body", # gh release view body (should NOT be called in dry run)
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.9.1"], {"dry-run": True})

        output = mock_stdout.getvalue()
        self.assertIn("Would mark v0.9.1 as pre-release", output)

        # gh release edit should NOT be called
        edit_calls = [c for c in mock_run.call_args_list
                      if len(c[0]) >= 2 and "edit" in c[0][1]]
        self.assertEqual(len(edit_calls), 0)

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_hard_dry_run(self, mock_run, _gh_inst, _gh_auth):
        """Hard dry run prints what would happen without deleting."""
        mock_run.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list (latest)
        ]

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd(["0.9.1"], {"hard": True, "dry-run": True})

        output = mock_stdout.getvalue()
        self.assertIn("Would delete GitHub Release v0.9.1", output)

        # gh release delete should NOT be called
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0]) >= 2 and "delete" in c[0][1]]
        self.assertEqual(len(delete_calls), 0)


class TestErrorCases(unittest.TestCase):
    """Verify error handling for non-existent releases and latest-release guard."""

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_nonexistent_release(self, mock_run, _gh_inst, _gh_auth):
        """Yanking a non-existent release prints an error and exits."""
        mock_run.side_effect = Exception("release not found")

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["0.99.0"], {})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", mock_stderr.getvalue())

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_latest_release_blocked(self, mock_run, _gh_inst, _gh_auth):
        """Yanking the latest release is blocked with a suggestion to use undo."""
        mock_run.side_effect = [
            "",         # gh release view v1.0.0 (exists)
            "v1.0.0",  # gh release list (latest IS our target)
        ]

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                run_cmd(["1.0.0"], {})

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("latest release", mock_stderr.getvalue())
        self.assertIn("rlsbl undo", mock_stderr.getvalue())


class TestVersionNormalization(unittest.TestCase):
    """Verify that both '0.9.1' and 'v0.9.1' produce the same tag."""

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_without_v_prefix(self, mock_run, _gh_inst, _gh_auth):
        """'0.9.1' is normalized to tag 'v0.9.1'."""
        mock_run.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list
            "",         # gh release delete
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(["0.9.1"], {"hard": True, "yes": True})

        mock_run.assert_any_call("gh", ["release", "view", "v0.9.1"])
        mock_run.assert_any_call("gh", ["release", "delete", "v0.9.1", "--yes"])

    @patch("rlsbl.commands.yank.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.yank.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.yank.run")
    def test_with_v_prefix(self, mock_run, _gh_inst, _gh_auth):
        """'v0.9.1' is also normalized to tag 'v0.9.1'."""
        mock_run.side_effect = [
            "",         # gh release view v0.9.1
            "v0.9.2",  # gh release list
            "",         # gh release delete
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(["v0.9.1"], {"hard": True, "yes": True})

        mock_run.assert_any_call("gh", ["release", "view", "v0.9.1"])
        mock_run.assert_any_call("gh", ["release", "delete", "v0.9.1", "--yes"])


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


if __name__ == "__main__":
    unittest.main()
