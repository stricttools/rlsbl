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


class TestExclusionCleanupIntegration:
    """Integration test: finalize_version + clean_stale_exclusions together.

    Simulates the release flow where unreleased.jsonl is finalized into a
    versioned file, then stale exclusions referencing version="unreleased"
    are cleaned from config.json.
    """

    def test_finalize_then_cleanup(self, tmp_path):
        """After finalize_version renames unreleased.jsonl, exclusions
        referencing version='unreleased' become stale and are removed."""
        from rlsbl.changelog.files import finalize_version

        # Set up .rlsbl/changes/ with unreleased.jsonl containing entries
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased_jsonl = changes_dir / "unreleased.jsonl"
        unreleased_jsonl.write_text(
            '{"commits":["abc1234"],"user_facing":true,'
            '"description":"Added feature X","type":"feature"}\n'
            '{"commits":["def5678"],"user_facing":false}\n'
        )

        # Set up .rlsbl/config.json with a stale exclusion
        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_config(config_path, {
            "private": False,
            "batch_limits": {
                "max_commits_per_entry": 5,
                "max_entries_per_commit": 5,
                "exclusions": [
                    {
                        "reason": "temporary exclusion for large refactor",
                        "entries": [{"version": "unreleased", "line": 1}],
                    },
                    {
                        "reason": "permanent exclusion for v1.0.0",
                        "entries": [{"version": "1.0.0", "line": 3}],
                    },
                ],
            },
        })

        # Step 1: Finalize the version (mimics release flow step 14-15)
        finalize_version(str(changes_dir), "2.0.0")

        # Verify unreleased.jsonl was renamed to 2.0.0.jsonl
        assert (changes_dir / "2.0.0.jsonl").exists()
        assert (changes_dir / "unreleased.jsonl").exists()  # new empty file
        assert (changes_dir / "unreleased.jsonl").read_text() == ""

        # Verify the versioned file is read-only
        mode = (changes_dir / "2.0.0.jsonl").stat().st_mode
        assert not (mode & 0o222)  # no write bits

        # Step 2: Clean stale exclusions (mimics release flow after finalize)
        removed = clean_stale_exclusions(str(config_path))

        # Verify the unreleased exclusion was removed
        assert removed == 1
        result = _read_config(config_path)
        exclusions = result["batch_limits"]["exclusions"]
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "permanent exclusion for v1.0.0"

        # Verify other config keys are preserved
        assert result["private"] is False
        assert result["batch_limits"]["max_commits_per_entry"] == 5

    def test_finalize_then_cleanup_no_stale(self, tmp_path):
        """When config has no unreleased exclusions, cleanup is a no-op."""
        from rlsbl.changelog.files import finalize_version

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased_jsonl = changes_dir / "unreleased.jsonl"
        unreleased_jsonl.write_text(
            '{"commits":["abc1234"],"user_facing":true,'
            '"description":"Bug fix","type":"fix"}\n'
        )

        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_config(config_path, {
            "private": False,
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "released exclusion",
                        "entries": [{"version": "1.0.0", "line": 2}],
                    },
                ],
            },
        })

        finalize_version(str(changes_dir), "1.1.0")
        removed = clean_stale_exclusions(str(config_path))

        assert removed == 0
        result = _read_config(config_path)
        assert len(result["batch_limits"]["exclusions"]) == 1

    def test_finalize_then_cleanup_all_stale(self, tmp_path):
        """When all exclusions are stale, the list is emptied."""
        from rlsbl.changelog.files import finalize_version

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased_jsonl = changes_dir / "unreleased.jsonl"
        unreleased_jsonl.write_text(
            '{"commits":["aaa1111"],"user_facing":false}\n'
        )

        config_path = tmp_path / ".rlsbl" / "config.json"
        _write_config(config_path, {
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "stale exclusion A",
                        "entries": [{"version": "unreleased", "line": 1}],
                    },
                    {
                        "reason": "stale exclusion B",
                        "entries": [{"version": "unreleased", "line": 3}],
                    },
                ],
            },
        })

        finalize_version(str(changes_dir), "3.0.0")
        removed = clean_stale_exclusions(str(config_path))

        assert removed == 2
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == []


class TestCleanStaleCommitLevelExclusions:
    """Tests for commit-level exclusion cleanup.

    Commit-level exclusions have a "commits" key with commit hashes.
    They are stale when ALL referenced commits are no longer in
    unreleased.jsonl (moved to a versioned file during finalization).
    """

    def _setup_changes_dir(self, tmp_path, unreleased_content=""):
        """Create .rlsbl/changes/ with unreleased.jsonl."""
        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text(unreleased_content)
        return changes_dir

    def _setup_config(self, tmp_path, config):
        """Write .rlsbl/config.json and return its path."""
        config_path = tmp_path / ".rlsbl" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write_config(config_path, config)
        return config_path

    def test_removes_commit_exclusion_when_all_commits_released(self, tmp_path):
        """After finalization, unreleased.jsonl is empty so all commit-level
        exclusions become stale."""
        self._setup_changes_dir(tmp_path, unreleased_content="")
        config_path = self._setup_config(tmp_path, {
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "large refactor",
                        "commits": ["abc1234", "def5678"],
                    }
                ]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 1
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == []

    def test_preserves_commit_exclusion_when_commits_still_unreleased(self, tmp_path):
        """Commit-level exclusion is kept if any commit is still in
        unreleased.jsonl."""
        self._setup_changes_dir(
            tmp_path,
            '{"commits":["abc1234"],"user_facing":false}\n'
        )
        exclusion = {
            "reason": "large refactor",
            "commits": ["abc1234", "def5678"],
        }
        config_path = self._setup_config(tmp_path, {
            "batch_limits": {
                "exclusions": [exclusion]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 0
        result = _read_config(config_path)
        assert len(result["batch_limits"]["exclusions"]) == 1

    def test_removes_only_stale_commit_exclusions(self, tmp_path):
        """Mixed: one stale commit-level exclusion, one still active."""
        self._setup_changes_dir(
            tmp_path,
            '{"commits":["still_here"],"user_facing":false}\n'
        )
        stale = {
            "reason": "released commits",
            "commits": ["gone1", "gone2"],
        }
        active = {
            "reason": "still unreleased",
            "commits": ["still_here"],
        }
        config_path = self._setup_config(tmp_path, {
            "batch_limits": {
                "exclusions": [stale, active]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 1
        result = _read_config(config_path)
        assert len(result["batch_limits"]["exclusions"]) == 1
        assert result["batch_limits"]["exclusions"][0]["reason"] == "still unreleased"

    def test_mixed_entry_and_commit_exclusions(self, tmp_path):
        """Both entry-level and commit-level stale exclusions are removed."""
        self._setup_changes_dir(tmp_path, unreleased_content="")
        entry_exclusion = {
            "reason": "stale entry",
            "entries": [{"version": "unreleased", "line": 1}],
        }
        commit_exclusion = {
            "reason": "stale commits",
            "commits": ["abc1234"],
        }
        released_entry = {
            "reason": "released entry",
            "entries": [{"version": "1.0.0", "line": 3}],
        }
        config_path = self._setup_config(tmp_path, {
            "batch_limits": {
                "exclusions": [entry_exclusion, commit_exclusion, released_entry]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 2
        result = _read_config(config_path)
        assert len(result["batch_limits"]["exclusions"]) == 1
        assert result["batch_limits"]["exclusions"][0]["reason"] == "released entry"

    def test_no_unreleased_jsonl_file(self, tmp_path):
        """When unreleased.jsonl does not exist, commit-level exclusions
        are all treated as stale (no unreleased commits to match)."""
        # Create .rlsbl/ but no changes/ directory
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir(parents=True)
        config_path = self._setup_config(tmp_path, {
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "orphaned",
                        "commits": ["abc1234"],
                    }
                ]
            }
        })

        removed = clean_stale_exclusions(str(config_path))

        assert removed == 1
        result = _read_config(config_path)
        assert result["batch_limits"]["exclusions"] == []

    def test_finalize_then_cleanup_removes_commit_exclusions(self, tmp_path):
        """Integration: after finalize_version, commit-level exclusions
        for the released commits are cleaned up."""
        from rlsbl.changelog.files import finalize_version

        changes_dir = tmp_path / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        unreleased_jsonl = changes_dir / "unreleased.jsonl"
        unreleased_jsonl.write_text(
            '{"commits":["abc1234"],"user_facing":true,'
            '"description":"Feature X","type":"feature"}\n'
            '{"commits":["def5678"],"user_facing":false}\n'
        )

        config_path = self._setup_config(tmp_path, {
            "private": False,
            "batch_limits": {
                "exclusions": [
                    {
                        "reason": "commit-level for released",
                        "commits": ["abc1234", "def5678"],
                    },
                    {
                        "reason": "entry-level for released",
                        "entries": [{"version": "unreleased", "line": 1}],
                    },
                    {
                        "reason": "permanent released entry",
                        "entries": [{"version": "1.0.0", "line": 3}],
                    },
                ],
            },
        })

        # Finalize: moves unreleased.jsonl to 2.0.0.jsonl, creates empty unreleased.jsonl
        finalize_version(str(changes_dir), "2.0.0")

        # After finalization, unreleased.jsonl is empty
        assert (changes_dir / "unreleased.jsonl").read_text() == ""

        removed = clean_stale_exclusions(str(config_path))

        # Both the commit-level and entry-level stale exclusions removed
        assert removed == 2
        result = _read_config(config_path)
        exclusions = result["batch_limits"]["exclusions"]
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "permanent released entry"
