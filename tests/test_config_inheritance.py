"""Tests for config inheritance: merge_config and releasable-level config loading."""

import json
import os

import pytest

from rlsbl.config import merge_config


class TestMergeConfigShallow:
    """merge_config replaces top-level scalar and list values from overlay."""

    def test_overlay_replaces_scalar(self):
        base = {"private": True, "push_timeout": 120}
        overlay = {"private": False}
        result = merge_config(base, overlay)
        assert result == {"private": False, "push_timeout": 120}

    def test_overlay_replaces_list(self):
        base = {"targets": ["pypi"]}
        overlay = {"targets": ["npm", "go"]}
        result = merge_config(base, overlay)
        assert result == {"targets": ["npm", "go"]}

    def test_overlay_adds_new_keys(self):
        base = {"private": True}
        overlay = {"push_timeout": 300}
        result = merge_config(base, overlay)
        assert result == {"private": True, "push_timeout": 300}


class TestMergeConfigDeep:
    """merge_config deep-merges nested dicts."""

    def test_nested_dict_merged(self):
        base = {"batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"batch_limits": {"max_entries_per_commit": 3}}
        result = merge_config(base, overlay)
        assert result == {
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 3,
            }
        }

    def test_nested_dict_overlay_replaces_scalar_in_nested(self):
        base = {"batch_limits": {"max_commits_per_entry": 5, "max_entries_per_commit": 2}}
        overlay = {"batch_limits": {"max_commits_per_entry": 10}}
        result = merge_config(base, overlay)
        assert result == {
            "batch_limits": {
                "max_commits_per_entry": 10,
                "max_entries_per_commit": 2,
            }
        }

    def test_non_dict_replaces_dict(self):
        """When overlay has a non-dict where base has a dict, overlay wins."""
        base = {"pipelines": {"pypi": {"type": "pypi"}}}
        overlay = {"pipelines": "none"}
        result = merge_config(base, overlay)
        assert result == {"pipelines": "none"}

    def test_dict_replaces_non_dict(self):
        """When overlay has a dict where base has a non-dict, overlay wins."""
        base = {"pipelines": "none"}
        overlay = {"pipelines": {"pypi": {"type": "pypi"}}}
        result = merge_config(base, overlay)
        assert result == {"pipelines": {"pypi": {"type": "pypi"}}}


class TestMergeConfigMissingKeys:
    """merge_config preserves base keys absent from overlay."""

    def test_base_preserved_when_overlay_empty(self):
        base = {"private": True, "push_timeout": 120, "targets": ["pypi"]}
        overlay = {}
        result = merge_config(base, overlay)
        assert result == base

    def test_base_preserved_for_absent_keys(self):
        base = {"private": True, "push_timeout": 120, "batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"private": False}
        result = merge_config(base, overlay)
        assert result == {
            "private": False,
            "push_timeout": 120,
            "batch_limits": {"max_commits_per_entry": 5},
        }

    def test_no_mutation_of_inputs(self):
        base = {"batch_limits": {"max_commits_per_entry": 5}}
        overlay = {"batch_limits": {"max_entries_per_commit": 3}}
        base_copy = json.loads(json.dumps(base))
        overlay_copy = json.loads(json.dumps(overlay))
        merge_config(base, overlay)
        assert base == base_copy
        assert overlay == overlay_copy
