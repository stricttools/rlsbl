"""Tests for check-name --target repeatable flag behavior."""

import unittest
from unittest.mock import patch
from conftest import cli_ctx


class TestCheckNameMultiTarget(unittest.TestCase):
    """Verify that --target can be specified multiple times."""

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_multiple_targets_all_checked(self, mock_run_cmd):
        """Passing --target npm --target pypi should call run_cmd for both."""
        from rlsbl import cmd_check_name

        mock_run_cmd.return_value = (0, [])
        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(cli_ctx(json=False), target=["npm", "pypi"], delay="200")
        self.assertEqual(cm.exception.code, 0)

        self.assertEqual(mock_run_cmd.call_count, 2)
        # First call with npm
        self.assertEqual(mock_run_cmd.call_args_list[0][0][0], "npm")
        self.assertEqual(mock_run_cmd.call_args_list[0][0][1], ["mypackage"])
        # Second call with pypi
        self.assertEqual(mock_run_cmd.call_args_list[1][0][0], "pypi")
        self.assertEqual(mock_run_cmd.call_args_list[1][0][1], ["mypackage"])

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_multi_target_exits_with_highest_code(self, mock_run_cmd):
        """The handler exits with the highest exit code across all targets."""
        from rlsbl import cmd_check_name

        # npm available (0), pypi taken (1) -> overall exit 1
        mock_run_cmd.side_effect = [(0, []), (1, [])]
        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(cli_ctx(json=False), target=["npm", "pypi"], delay="200")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(mock_run_cmd.call_count, 2)

    def test_invalid_target_hard_error(self):
        """An unknown registry is refused by the declaration, at parse time.

        `--target` carries `choices=[Choice("npm"), ...]` since the strictcli
        0.41 migration, so the handler never runs and never re-validates.
        """
        from rlsbl import app

        result = app.test(["check-name", "mypackage", "--target", "invalid"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid", result.stderr)
        self.assertIn("npm, pypi, go, github", result.stderr)

    def test_invalid_target_reported_on_the_first_bad_value(self):
        """The parser refuses the first value that is not a declared choice."""
        from rlsbl import app

        result = app.test([
            "check-name", "mypackage", "--target", "bad1", "--target", "bad2",
        ])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("bad1", result.stderr)

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_single_target_still_works(self, mock_run_cmd):
        """Passing a single --target pypi should only call run_cmd once."""
        from rlsbl import cmd_check_name

        mock_run_cmd.return_value = (0, [])
        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(cli_ctx(json=False), target=["pypi"], delay="200")
        self.assertEqual(cm.exception.code, 0)

        self.assertEqual(mock_run_cmd.call_count, 1)
        self.assertEqual(mock_run_cmd.call_args_list[0][0][0], "pypi")

    def test_empty_target_list_errors(self):
        """No --target at all is the declaration's refusal, not the handler's.

        `presence="required"` on a repeatable flag demands at least one
        occurrence from some source.
        """
        from rlsbl import app

        result = app.test(["check-name", "mypackage"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--target", result.stderr)


if __name__ == "__main__":
    unittest.main()
