"""Tests for the get_changelog_validation_config() helper.

Verifies that the helper reads `.rlsbl/config.json` correctly, returning
the `batch_limits` section as a dict and falling back to an empty dict
when the file is missing, the section is absent, or the value is malformed.

The helper is a pure-addition Phase 0 building block; Phase 4 will use it
to drive the changelog validator's batch limits.
"""

import json

from rlsbl.config import get_changelog_validation_config


def _write_config(tmp_path, payload):
    """Write `.rlsbl/config.json` in tmp_path with the given dict payload."""
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(payload))


class TestGetChangelogValidationConfig:
    def test_empty_when_config_file_missing(self, tmp_project):
        # No .rlsbl/config.json at all
        assert get_changelog_validation_config() == {}

    def test_empty_when_batch_limits_absent(self, tmp_project):
        _write_config(tmp_project, {"publish": {"pypi": {"local": True}}})
        assert get_changelog_validation_config() == {}

    def test_empty_when_batch_limits_not_dict(self, tmp_project):
        _write_config(tmp_project, {"batch_limits": "not-a-dict"})
        assert get_changelog_validation_config() == {}

    def test_empty_when_batch_limits_is_list(self, tmp_project):
        _write_config(tmp_project, {"batch_limits": [1, 2, 3]})
        assert get_changelog_validation_config() == {}

    def test_returns_configured_dict(self, tmp_project):
        payload = {
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 2,
                "excluded_hashes": ["abc123", "def456"],
                "excluded_lines": ["some.*pattern"],
            }
        }
        _write_config(tmp_project, payload)
        assert get_changelog_validation_config() == payload["batch_limits"]

    def test_returns_partial_dict_without_applying_defaults(self, tmp_project):
        """Caller is responsible for per-key defaults; helper passes through as-is."""
        _write_config(tmp_project, {"batch_limits": {"max_commits_per_entry": 7}})
        assert get_changelog_validation_config() == {"max_commits_per_entry": 7}
