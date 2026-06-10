"""Tests for clean_stale_exclusions() — auto-cleanup of batch_limits
exclusions that reference version="unreleased" after release finalization."""

from __future__ import annotations

import json

import pytest

from rlsbl.config import clean_stale_exclusions


def _write_config(path, config):
    path.write_text(json.dumps(config, indent=2) + "\n")


def _read_config(path):
    return json.loads(path.read_text())


class TestCleanStaleExclusions:

    def test_cleanup_removes_unreleased_exclusion(self, tmp_path):
        config_path = tmp_path / "config.json"
        _write_config(config_path, {
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "some reason",
                        "entries": [{"version": "unreleased", "line": 1}],
                    }
                ]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 1
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == []

    def test_cleanup_preserves_released_exclusion(self, tmp_path):
        config_path = tmp_path / "config.json"
        exclusion = {
            "reason": "released entry",
            "entries": [{"version": "1.0.0", "line": 3}],
        }
        _write_config(config_path, {
            "batch_limits": {
                "exclusions": [exclusion]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 0
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == [exclusion]

    def test_cleanup_handles_no_exclusions(self, tmp_path):
        config_path = tmp_path / "config.json"
        _write_config(config_path, {
            "batch_limits": {"exclusions": []}
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 0
        # File should not have been rewritten (check by comparing content)
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == []

    def test_cleanup_handles_no_batch_limits(self, tmp_path):
        config_path = tmp_path / "config.json"
        _write_config(config_path, {"private": False})

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 0
        result = _read_config(config_path)
        assert "batch_limits" not in result

    def test_cleanup_removes_multiple_unreleased(self, tmp_path):
        config_path = tmp_path / "config.json"
        unreleased_1 = {
            "reason": "first unreleased",
            "entries": [{"version": "unreleased", "line": 1}],
        }
        unreleased_2 = {
            "reason": "second unreleased",
            "entries": [{"version": "unreleased", "line": 5}],
        }
        released = {
            "reason": "released entry",
            "entries": [{"version": "2.0.0", "line": 2}],
        }
        _write_config(config_path, {
            "batch_limits": {
                "exclusions": [unreleased_1, released, unreleased_2]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 2
        result = _read_config(config_path)
        assert len(result["batch_limits"]["exclusions"]) == 1
        assert result["batch_limits"]["exclusions"][0]["reason"] == "released entry"
