"""Tests for rlsbl.commands.release.release_state — state file infrastructure."""

import json
import os

import pytest

from rlsbl.commands.release.release_state import (
    clear_release_state,
    get_state_path,
    load_release_state,
    save_release_state,
    save_step,
)


class TestSaveLoadRoundTrip:
    """save_release_state + load_release_state round-trips correctly."""

    def test_round_trip(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        state = {
            "new_version": "1.2.3",
            "tag": "v1.2.3",
            "branch": "main",
            "pre_release_sha": "abc123def456",
            "bump_type": "patch",
            "registry": "pypi",
            "completed_steps": [],
            "companion_tags": [],
            "monorepo_name": None,
            "releasable_name": None,
        }
        save_release_state(state_path, state)
        loaded = load_release_state(state_path)
        assert loaded == state

    def test_round_trip_with_completed_steps(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        state = {
            "new_version": "2.0.0",
            "completed_steps": ["VERSION_BUMPED", "COMMITTED"],
        }
        save_release_state(state_path, state)
        loaded = load_release_state(state_path)
        assert loaded["completed_steps"] == ["VERSION_BUMPED", "COMMITTED"]


class TestSaveStep:
    """save_step appends a step name to completed_steps."""

    def test_appends_step(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {
            "new_version": "1.0.0",
            "completed_steps": [],
        })
        save_step(state_path, "VERSION_BUMPED")
        loaded = load_release_state(state_path)
        assert loaded["completed_steps"] == ["VERSION_BUMPED"]

    def test_appends_multiple_steps(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {
            "new_version": "1.0.0",
            "completed_steps": [],
        })
        save_step(state_path, "VERSION_BUMPED")
        save_step(state_path, "COMMITTED")
        save_step(state_path, "TAGGED")
        loaded = load_release_state(state_path)
        assert loaded["completed_steps"] == ["VERSION_BUMPED", "COMMITTED", "TAGGED"]

    def test_does_not_duplicate_step(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {
            "new_version": "1.0.0",
            "completed_steps": ["VERSION_BUMPED"],
        })
        save_step(state_path, "VERSION_BUMPED")
        loaded = load_release_state(state_path)
        assert loaded["completed_steps"] == ["VERSION_BUMPED"]

    def test_preserves_other_fields(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {
            "new_version": "3.0.0",
            "tag": "v3.0.0",
            "completed_steps": [],
        })
        save_step(state_path, "PUSHED")
        loaded = load_release_state(state_path)
        assert loaded["new_version"] == "3.0.0"
        assert loaded["tag"] == "v3.0.0"
        assert loaded["completed_steps"] == ["PUSHED"]


class TestAtomicWrite:
    """save_release_state uses tmp file + os.replace for atomicity."""

    def test_no_partial_writes(self, tmp_path):
        """After save, the file is a valid JSON document."""
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": ["A"]})
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"completed_steps": ["A"]}

    def test_no_leftover_tmp_files(self, tmp_path):
        """No .tmp files remain after a successful save."""
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        tmp_files = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
        assert tmp_files == []

    def test_creates_parent_directories(self, tmp_path):
        """save_release_state creates parent dirs if they don't exist."""
        state_path = str(tmp_path / "nested" / "dir" / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        loaded = load_release_state(state_path)
        assert loaded == {"completed_steps": []}


class TestClearReleaseState:
    """clear_release_state deletes the file."""

    def test_deletes_file(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        assert os.path.exists(state_path)
        clear_release_state(state_path)
        assert not os.path.exists(state_path)

    def test_no_error_when_missing(self, tmp_path):
        state_path = str(tmp_path / "nonexistent.json")
        # Should not raise
        clear_release_state(state_path)


class TestLoadReturnsNoneWhenMissing:
    """load_release_state returns None when the file does not exist."""

    def test_returns_none(self, tmp_path):
        state_path = str(tmp_path / "missing.json")
        assert load_release_state(state_path) is None


class TestGetStatePath:
    """get_state_path returns the correct path."""

    def test_path(self):
        result = get_state_path("/project")
        assert result == "/project/.rlsbl/releases/in-progress.json"
