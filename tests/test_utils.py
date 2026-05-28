"""Tests for rlsbl.utils — bump_version, extract_changelog_entry, and timeout helpers."""

import json
import os
import shutil
import tempfile
import unittest

from rlsbl.utils import bump_version, extract_changelog_entry, get_hook_timeout, get_push_timeout


class TestBumpVersion(unittest.TestCase):
    """Tests for bump_version(version, bump_type)."""

    def test_patch_bump(self):
        self.assertEqual(bump_version("1.2.3", "patch"), "1.2.4")

    def test_minor_bump(self):
        self.assertEqual(bump_version("1.2.3", "minor"), "1.3.0")

    def test_major_bump(self):
        self.assertEqual(bump_version("1.2.3", "major"), "2.0.0")

    def test_0x_patch(self):
        self.assertEqual(bump_version("0.1.0", "patch"), "0.1.1")

    def test_0x_minor(self):
        self.assertEqual(bump_version("0.1.0", "minor"), "0.2.0")

    def test_0x_major(self):
        self.assertEqual(bump_version("0.1.0", "major"), "1.0.0")

    def test_invalid_version_raises(self):
        with self.assertRaises(ValueError):
            bump_version("not-a-version", "patch")

    def test_too_few_parts_raises(self):
        with self.assertRaises(ValueError):
            bump_version("1.2", "patch")

    def test_too_many_parts_raises(self):
        with self.assertRaises(ValueError):
            bump_version("1.2.3.4", "patch")

    def test_non_numeric_parts_raises(self):
        with self.assertRaises(ValueError):
            bump_version("1.2.x", "patch")

    def test_invalid_bump_type_raises(self):
        with self.assertRaises(ValueError):
            bump_version("1.2.3", "mega")

    # Pre-release suffix handling

    def test_prerelease_beta_patch(self):
        self.assertEqual(bump_version("1.0.0-beta.1", "patch"), "1.0.1")

    def test_prerelease_beta_minor(self):
        self.assertEqual(bump_version("1.0.0-beta.1", "minor"), "1.1.0")

    def test_prerelease_beta_major(self):
        self.assertEqual(bump_version("1.0.0-beta.1", "major"), "2.0.0")

    def test_prerelease_rc_patch(self):
        self.assertEqual(bump_version("2.3.0-rc.2", "patch"), "2.3.1")

    def test_prerelease_rc_minor(self):
        self.assertEqual(bump_version("2.3.0-rc.2", "minor"), "2.4.0")

    def test_prerelease_rc_major(self):
        self.assertEqual(bump_version("2.3.0-rc.2", "major"), "3.0.0")

    def test_prerelease_alpha(self):
        self.assertEqual(bump_version("0.5.0-alpha.3", "patch"), "0.5.1")

    def test_clean_version_still_works_after_prerelease_support(self):
        # Regression check: clean versions must remain unchanged
        self.assertEqual(bump_version("3.2.1", "patch"), "3.2.2")
        self.assertEqual(bump_version("3.2.1", "minor"), "3.3.0")
        self.assertEqual(bump_version("3.2.1", "major"), "4.0.0")


class TestExtractChangelogEntry(unittest.TestCase):
    """Tests for extract_changelog_entry(changelog_path, version)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def _write_changelog(self, content):
        """Helper: write content to a temp CHANGELOG.md and return its path."""
        path = os.path.join(self.tmp_dir, "CHANGELOG.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_extracts_entry_between_two_headers(self):
        path = self._write_changelog(
            "## 2.0.0\n\nNew stuff\n\n## 1.0.0\n\nOld stuff\n"
        )
        self.assertEqual(extract_changelog_entry(path, "2.0.0"), "New stuff")

    def test_extracts_entry_at_end_of_file(self):
        path = self._write_changelog(
            "## 2.0.0\n\nNew stuff\n\n## 1.0.0\n\nOld stuff\n"
        )
        self.assertEqual(extract_changelog_entry(path, "1.0.0"), "Old stuff")

    def test_returns_none_for_missing_version(self):
        path = self._write_changelog("## 1.0.0\n\nSome notes\n")
        self.assertIsNone(extract_changelog_entry(path, "9.9.9"))

    def test_does_not_match_version_prefix(self):
        # "1.0.0" header should NOT match when searching for "1.0.0-beta"
        path = self._write_changelog("## 1.0.0\n\nRelease notes\n")
        self.assertIsNone(extract_changelog_entry(path, "1.0.0-beta"))

    def test_does_not_match_version_suffix(self):
        # "1.0.0-beta" header should NOT match when searching for "1.0.0"
        path = self._write_changelog("## 1.0.0-beta\n\nBeta notes\n")
        self.assertIsNone(extract_changelog_entry(path, "1.0.0"))

    def test_handles_empty_body(self):
        path = self._write_changelog("## 1.0.0\n\n## 0.9.0\n\nOlder\n")
        self.assertIsNone(extract_changelog_entry(path, "1.0.0"))

    def test_handles_multiline_entries(self):
        path = self._write_changelog(
            "## 1.0.0\n\n- Feature A\n- Feature B\n- Feature C\n"
        )
        self.assertEqual(
            extract_changelog_entry(path, "1.0.0"),
            "- Feature A\n- Feature B\n- Feature C",
        )


class TestGetHookTimeout(unittest.TestCase):
    """Tests for get_hook_timeout()."""

    def test_no_env_var_returns_none(self):
        os.environ.pop("RLSBL_HOOK_TIMEOUT", None)
        self.assertIsNone(get_hook_timeout())

    def test_valid_value_returns_int(self):
        os.environ["RLSBL_HOOK_TIMEOUT"] = "30"
        try:
            self.assertEqual(get_hook_timeout(), 30)
        finally:
            del os.environ["RLSBL_HOOK_TIMEOUT"]

    def test_invalid_string_warns_and_returns_none(self, ):
        os.environ["RLSBL_HOOK_TIMEOUT"] = "not-a-number"
        try:
            import io
            import sys
            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured
            try:
                result = get_hook_timeout()
            finally:
                sys.stderr = old_stderr
            self.assertIsNone(result)
            self.assertIn("invalid RLSBL_HOOK_TIMEOUT", captured.getvalue())
        finally:
            del os.environ["RLSBL_HOOK_TIMEOUT"]

    def test_zero_warns_and_returns_none(self):
        os.environ["RLSBL_HOOK_TIMEOUT"] = "0"
        try:
            import io
            import sys
            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured
            try:
                result = get_hook_timeout()
            finally:
                sys.stderr = old_stderr
            self.assertIsNone(result)
            self.assertIn("invalid RLSBL_HOOK_TIMEOUT", captured.getvalue())
        finally:
            del os.environ["RLSBL_HOOK_TIMEOUT"]

    def test_negative_warns_and_returns_none(self):
        os.environ["RLSBL_HOOK_TIMEOUT"] = "-5"
        try:
            import io
            import sys
            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured
            try:
                result = get_hook_timeout()
            finally:
                sys.stderr = old_stderr
            self.assertIsNone(result)
            self.assertIn("invalid RLSBL_HOOK_TIMEOUT", captured.getvalue())
        finally:
            del os.environ["RLSBL_HOOK_TIMEOUT"]


class TestGetPushTimeout(unittest.TestCase):
    """Tests for get_push_timeout() — env var > config > error."""

    def setUp(self):
        # Save and clear env var
        self._saved_env = os.environ.pop("RLSBL_PUSH_TIMEOUT", None)
        # Save cwd and switch to a temp dir with .rlsbl/config.json
        self._orig_cwd = os.getcwd()
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmp_dir)
        if self._saved_env is not None:
            os.environ["RLSBL_PUSH_TIMEOUT"] = self._saved_env
        else:
            os.environ.pop("RLSBL_PUSH_TIMEOUT", None)

    def _write_config(self, data):
        """Write a .rlsbl/config.json in the temp dir."""
        config_dir = os.path.join(self._tmp_dir, ".rlsbl")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            json.dump(data, f)

    def test_env_var_returns_value(self):
        os.environ["RLSBL_PUSH_TIMEOUT"] = "60"
        os.chdir(self._tmp_dir)
        self.assertEqual(get_push_timeout(), 60)

    def test_config_returns_value(self):
        self._write_config({"push_timeout": 90})
        os.chdir(self._tmp_dir)
        self.assertEqual(get_push_timeout(), 90)

    def test_neither_raises_error(self):
        self._write_config({})
        os.chdir(self._tmp_dir)
        with self.assertRaises(ValueError) as ctx:
            get_push_timeout()
        self.assertIn("push_timeout not configured", str(ctx.exception))

    def test_env_var_takes_precedence_over_config(self):
        os.environ["RLSBL_PUSH_TIMEOUT"] = "45"
        self._write_config({"push_timeout": 200})
        os.chdir(self._tmp_dir)
        self.assertEqual(get_push_timeout(), 45)

    def test_invalid_env_var_raises_error(self):
        os.environ["RLSBL_PUSH_TIMEOUT"] = "not-a-number"
        os.chdir(self._tmp_dir)
        with self.assertRaises(ValueError) as ctx:
            get_push_timeout()
        self.assertIn("Invalid RLSBL_PUSH_TIMEOUT", str(ctx.exception))

    def test_zero_env_var_raises_error(self):
        os.environ["RLSBL_PUSH_TIMEOUT"] = "0"
        os.chdir(self._tmp_dir)
        with self.assertRaises(ValueError) as ctx:
            get_push_timeout()
        self.assertIn("Invalid RLSBL_PUSH_TIMEOUT", str(ctx.exception))

    def test_invalid_config_value_raises_error(self):
        self._write_config({"push_timeout": "slow"})
        os.chdir(self._tmp_dir)
        with self.assertRaises(ValueError) as ctx:
            get_push_timeout()
        self.assertIn("Invalid push_timeout", str(ctx.exception))

    def test_negative_config_value_raises_error(self):
        self._write_config({"push_timeout": -10})
        os.chdir(self._tmp_dir)
        with self.assertRaises(ValueError) as ctx:
            get_push_timeout()
        self.assertIn("Invalid push_timeout", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
