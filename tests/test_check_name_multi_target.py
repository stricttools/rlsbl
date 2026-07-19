"""Tests for check-name --target repeatable flag behavior."""

import sys
import unittest
from unittest.mock import patch, MagicMock


class TestCheckNameMultiTarget(unittest.TestCase):
    """Verify that --target can be specified multiple times."""

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_multiple_targets_all_checked(self, mock_run_cmd):
        """Passing --target npm --target pypi should call run_cmd for both."""
        from rlsbl import cmd_check_name

        mock_run_cmd.return_value = (0, [])
        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(None, target=["npm", "pypi"], delay="200", json=False)
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
            cmd_check_name(None, target=["npm", "pypi"], delay="200", json=False)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(mock_run_cmd.call_count, 2)

    def test_invalid_target_hard_error(self):
        """Passing --target invalid should sys.exit(1) with error message."""
        from rlsbl import cmd_check_name

        with patch("sys.stderr") as mock_stderr:
            with self.assertRaises(SystemExit) as cm:
                cmd_check_name(None, target=["invalid"], delay="200", json=False)
            self.assertEqual(cm.exception.code, 1)

    def test_multiple_invalid_targets_listed(self):
        """All invalid targets should be listed in the error message."""
        from rlsbl import cmd_check_name
        from io import StringIO

        captured = StringIO()
        with patch("sys.stderr", captured):
            with self.assertRaises(SystemExit) as cm:
                cmd_check_name(None, target=["bad1", "bad2"], delay="200", json=False)
            self.assertEqual(cm.exception.code, 1)
        output = captured.getvalue()
        self.assertIn("'bad1'", output)
        self.assertIn("'bad2'", output)

    @patch("rlsbl.commands.check.run_cmd")
    @patch("rlsbl._variadic_args", ["mypackage"])
    def test_single_target_still_works(self, mock_run_cmd):
        """Passing a single --target pypi should only call run_cmd once."""
        from rlsbl import cmd_check_name

        mock_run_cmd.return_value = (0, [])
        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(None, target=["pypi"], delay="200", json=False)
        self.assertEqual(cm.exception.code, 0)

        self.assertEqual(mock_run_cmd.call_count, 1)
        self.assertEqual(mock_run_cmd.call_args_list[0][0][0], "pypi")

    def test_empty_target_list_errors(self):
        """Passing no --target should sys.exit(1)."""
        from rlsbl import cmd_check_name

        with self.assertRaises(SystemExit) as cm:
            cmd_check_name(None, target=[], delay="200", json=False)
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
