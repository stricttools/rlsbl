"""Tests for the get_changelog_validation_config() helper.

Verifies that the helper reads the ``batch_limits`` section from a config
dict correctly, returning it as a dict and falling back to an empty dict
when the section is absent or the value is malformed.
"""

import pytest

from rlsbl.config import get_changelog_validation_config
from rlsbl.errors import ConfigError


class TestGetChangelogValidationConfig:
    def test_empty_when_config_empty(self):
        assert get_changelog_validation_config({}) == {}

    def test_empty_when_batch_limits_absent(self):
        assert get_changelog_validation_config({"publish": {"pypi": {"local": True}}}) == {}

    def test_raises_when_batch_limits_not_dict(self):
        """A present-but-non-dict batch_limits is a hard error, not silently {}."""
        with pytest.raises(ConfigError) as exc_info:
            get_changelog_validation_config({"batch_limits": "not-a-dict"})
        msg = str(exc_info.value)
        assert "batch_limits" in msg
        assert "not-a-dict" in msg

    def test_raises_when_batch_limits_is_list(self):
        with pytest.raises(ConfigError) as exc_info:
            get_changelog_validation_config({"batch_limits": [1, 2, 3]})
        assert "batch_limits" in str(exc_info.value)

    def test_returns_configured_dict(self):
        config = {
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 2,
                "excluded_hashes": ["abc123", "def456"],
                "excluded_lines": ["some.*pattern"],
            }
        }
        assert get_changelog_validation_config(config) == config["batch_limits"]

    def test_returns_partial_dict_without_applying_defaults(self):
        """Caller is responsible for per-key defaults; helper passes through as-is."""
        config = {"batch_limits": {"max_commits_per_entry": 7}}
        assert get_changelog_validation_config(config) == {"max_commits_per_entry": 7}
