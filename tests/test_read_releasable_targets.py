"""Tests for read_releasable_targets loud-error behavior (audit follow-ups i, ii).

- An absent ``targets`` key returns None (callers fall through).
- A non-empty list is returned as-is.
- An empty list is a hard ConfigError.
- A present-but-non-list value (string/dict) is a hard ConfigError -- never
  silently treated as absent.
- The empty-targets ban message is a single shared string used by both
  config validation and releasable target resolution.
"""

import json
import os

import pytest

from rlsbl.errors import ConfigError
from rlsbl.targets import read_releasable_targets
from rlsbl.config import empty_targets_ban_message, non_list_targets_ban_message


def _write_config(tmp_path, data):
    path = os.path.join(str(tmp_path), "config.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


class TestReadReleasableTargets:
    def test_absent_returns_none(self, tmp_path):
        path = _write_config(tmp_path, {"publish_mode": "ci"})
        assert read_releasable_targets(path) is None

    def test_non_empty_list_returned(self, tmp_path):
        path = _write_config(tmp_path, {"targets": ["pypi", "npm"]})
        assert read_releasable_targets(path) == ["pypi", "npm"]

    def test_empty_list_hard_error(self, tmp_path):
        path = _write_config(tmp_path, {"targets": []})
        with pytest.raises(ConfigError, match="targets is an empty list"):
            read_releasable_targets(path)

    def test_string_value_hard_error(self, tmp_path):
        """A present-but-non-list targets (string) is a hard error, not absent."""
        path = _write_config(tmp_path, {"targets": "pypi"})
        with pytest.raises(ConfigError, match="targets must be a list"):
            read_releasable_targets(path)

    def test_dict_value_hard_error(self, tmp_path):
        path = _write_config(tmp_path, {"targets": {"name": "pypi"}})
        with pytest.raises(ConfigError, match="targets must be a list"):
            read_releasable_targets(path)


class TestSharedBanMessages:
    def test_empty_message_shared(self):
        # config-level and releasable-level differ only in the location string.
        assert empty_targets_ban_message("config").startswith("targets is an empty list in config.")
        assert 'set "publish_mode": "none"' in empty_targets_ban_message("/x/config.json")

    def test_non_list_message_names_type(self):
        assert "must be a list" in non_list_targets_ban_message("config", "pypi")
        assert "str" in non_list_targets_ban_message("config", "pypi")
