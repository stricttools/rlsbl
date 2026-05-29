"""Tests for rlsbl.utils -- bump_version, extract_changelog_entry, and timeout helpers."""

import json
import os

import pytest

from rlsbl.utils import bump_version, extract_changelog_entry, get_hook_timeout, get_push_timeout


class TestBumpVersion:
    """Tests for bump_version(version, bump_type)."""

    def test_patch_bump(self):
        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_minor_bump(self):
        assert bump_version("1.2.3", "minor") == "1.3.0"

    def test_major_bump(self):
        assert bump_version("1.2.3", "major") == "2.0.0"

    def test_0x_patch(self):
        assert bump_version("0.1.0", "patch") == "0.1.1"

    def test_0x_minor(self):
        assert bump_version("0.1.0", "minor") == "0.2.0"

    def test_0x_major(self):
        assert bump_version("0.1.0", "major") == "1.0.0"

    def test_invalid_version_raises(self):
        with pytest.raises(ValueError):
            bump_version("not-a-version", "patch")

    def test_too_few_parts_raises(self):
        with pytest.raises(ValueError):
            bump_version("1.2", "patch")

    def test_too_many_parts_raises(self):
        with pytest.raises(ValueError):
            bump_version("1.2.3.4", "patch")

    def test_non_numeric_parts_raises(self):
        with pytest.raises(ValueError):
            bump_version("1.2.x", "patch")

    def test_invalid_bump_type_raises(self):
        with pytest.raises(ValueError):
            bump_version("1.2.3", "mega")

    # Pre-release suffix handling

    def test_prerelease_beta_patch(self):
        assert bump_version("1.0.0-beta.1", "patch") == "1.0.1"

    def test_prerelease_beta_minor(self):
        assert bump_version("1.0.0-beta.1", "minor") == "1.1.0"

    def test_prerelease_beta_major(self):
        assert bump_version("1.0.0-beta.1", "major") == "2.0.0"

    def test_prerelease_rc_patch(self):
        assert bump_version("2.3.0-rc.2", "patch") == "2.3.1"

    def test_prerelease_rc_minor(self):
        assert bump_version("2.3.0-rc.2", "minor") == "2.4.0"

    def test_prerelease_rc_major(self):
        assert bump_version("2.3.0-rc.2", "major") == "3.0.0"

    def test_prerelease_alpha(self):
        assert bump_version("0.5.0-alpha.3", "patch") == "0.5.1"

    def test_clean_version_still_works_after_prerelease_support(self):
        # Regression check: clean versions must remain unchanged
        assert bump_version("3.2.1", "patch") == "3.2.2"
        assert bump_version("3.2.1", "minor") == "3.3.0"
        assert bump_version("3.2.1", "major") == "4.0.0"


class TestExtractChangelogEntry:
    """Tests for extract_changelog_entry(changelog_path, version)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_dir = str(tmp_path)

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
        assert extract_changelog_entry(path, "2.0.0") == "New stuff"

    def test_extracts_entry_at_end_of_file(self):
        path = self._write_changelog(
            "## 2.0.0\n\nNew stuff\n\n## 1.0.0\n\nOld stuff\n"
        )
        assert extract_changelog_entry(path, "1.0.0") == "Old stuff"

    def test_returns_none_for_missing_version(self):
        path = self._write_changelog("## 1.0.0\n\nSome notes\n")
        assert extract_changelog_entry(path, "9.9.9") is None

    def test_does_not_match_version_prefix(self):
        # "1.0.0" header should NOT match when searching for "1.0.0-beta"
        path = self._write_changelog("## 1.0.0\n\nRelease notes\n")
        assert extract_changelog_entry(path, "1.0.0-beta") is None

    def test_does_not_match_version_suffix(self):
        # "1.0.0-beta" header should NOT match when searching for "1.0.0"
        path = self._write_changelog("## 1.0.0-beta\n\nBeta notes\n")
        assert extract_changelog_entry(path, "1.0.0") is None

    def test_handles_empty_body(self):
        path = self._write_changelog("## 1.0.0\n\n## 0.9.0\n\nOlder\n")
        assert extract_changelog_entry(path, "1.0.0") is None

    def test_handles_multiline_entries(self):
        path = self._write_changelog(
            "## 1.0.0\n\n- Feature A\n- Feature B\n- Feature C\n"
        )
        assert extract_changelog_entry(path, "1.0.0") == "- Feature A\n- Feature B\n- Feature C"


class TestGetHookTimeout:
    """Tests for get_hook_timeout()."""

    def test_no_env_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("RLSBL_HOOK_TIMEOUT", raising=False)
        assert get_hook_timeout() is None

    def test_valid_value_returns_int(self, monkeypatch):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "30")
        assert get_hook_timeout() == 30

    def test_invalid_string_warns_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "not-a-number")
        import io
        import sys
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            result = get_hook_timeout()
        finally:
            sys.stderr = old_stderr
        assert result is None
        assert "invalid RLSBL_HOOK_TIMEOUT" in captured.getvalue()

    def test_zero_warns_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "0")
        import io
        import sys
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            result = get_hook_timeout()
        finally:
            sys.stderr = old_stderr
        assert result is None
        assert "invalid RLSBL_HOOK_TIMEOUT" in captured.getvalue()

    def test_negative_warns_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("RLSBL_HOOK_TIMEOUT", "-5")
        import io
        import sys
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            result = get_hook_timeout()
        finally:
            sys.stderr = old_stderr
        assert result is None
        assert "invalid RLSBL_HOOK_TIMEOUT" in captured.getvalue()


class TestGetPushTimeout:
    """Tests for get_push_timeout() -- env var > config dict > error."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        # Clear env var
        monkeypatch.delenv("RLSBL_PUSH_TIMEOUT", raising=False)

    def test_env_var_returns_value(self, monkeypatch):
        monkeypatch.setenv("RLSBL_PUSH_TIMEOUT", "60")
        assert get_push_timeout({}) == 60

    def test_config_returns_value(self):
        assert get_push_timeout({"push_timeout": 90}) == 90

    def test_neither_raises_error(self):
        with pytest.raises(ValueError) as exc_info:
            get_push_timeout({})
        assert "push_timeout not configured" in str(exc_info.value)

    def test_env_var_takes_precedence_over_config(self, monkeypatch):
        monkeypatch.setenv("RLSBL_PUSH_TIMEOUT", "45")
        assert get_push_timeout({"push_timeout": 200}) == 45

    def test_invalid_env_var_raises_error(self, monkeypatch):
        monkeypatch.setenv("RLSBL_PUSH_TIMEOUT", "not-a-number")
        with pytest.raises(ValueError) as exc_info:
            get_push_timeout({})
        assert "Invalid RLSBL_PUSH_TIMEOUT" in str(exc_info.value)

    def test_zero_env_var_raises_error(self, monkeypatch):
        monkeypatch.setenv("RLSBL_PUSH_TIMEOUT", "0")
        with pytest.raises(ValueError) as exc_info:
            get_push_timeout({})
        assert "Invalid RLSBL_PUSH_TIMEOUT" in str(exc_info.value)

    def test_invalid_config_value_raises_error(self):
        with pytest.raises(ValueError) as exc_info:
            get_push_timeout({"push_timeout": "slow"})
        assert "Invalid push_timeout" in str(exc_info.value)

    def test_negative_config_value_raises_error(self):
        with pytest.raises(ValueError) as exc_info:
            get_push_timeout({"push_timeout": -10})
        assert "Invalid push_timeout" in str(exc_info.value)
