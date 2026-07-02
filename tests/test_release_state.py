"""Tests for rlsbl.commands.release.release_state — state file infrastructure."""

import json
import os

import pytest

from rlsbl.commands.release.release_state import (
    FATAL_STEPS,
    MUTATING_STEPS,
    POST_RELEASE_STEPS,
    RELEASE_STEPS,
    clear_release_state,
    find_releasable_state_files,
    get_failed_steps,
    get_missing_steps,
    get_scrub_result_path,
    get_state_dir,
    get_state_path,
    has_fatal_failure,
    is_state_complete,
    load_release_state,
    save_release_state,
    save_step,
    save_step_failure,
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


class TestCanonicalStepList:
    """The ordered canonical step list is the single source of truth."""

    def test_mutating_steps_order(self):
        assert MUTATING_STEPS == (
            "VERSION_BUMPED",
            "COMMITTED",
            "CHANGELOG_FINALIZED",
            "RELEASE_FILE_FINALIZED",
            "TAGGED",
            "PUSHED",
            "GITHUB_RELEASE",
        )

    def test_post_release_steps_order(self):
        assert POST_RELEASE_STEPS == (
            "ASSETS_UPLOADED",
            "PIPELINES_PUBLISHED",
            "DEPLOYED",
            "POST_HOOKS_RUN",
            "SNAPSHOT_REGENERATED",
        )

    def test_release_steps_is_concatenation(self):
        assert RELEASE_STEPS == MUTATING_STEPS + POST_RELEASE_STEPS
        assert len(RELEASE_STEPS) == 12
        # No duplicates
        assert len(set(RELEASE_STEPS)) == len(RELEASE_STEPS)

    def test_fatal_classification(self):
        # All mutating steps are fatal on failure
        for step in MUTATING_STEPS:
            assert step in FATAL_STEPS
        # Asset upload and pipeline publish are fatal post-release steps
        assert "ASSETS_UPLOADED" in FATAL_STEPS
        assert "PIPELINES_PUBLISHED" in FATAL_STEPS
        # Deploy, post-hooks, and snapshot are non-fatal
        assert "DEPLOYED" not in FATAL_STEPS
        assert "POST_HOOKS_RUN" not in FATAL_STEPS
        assert "SNAPSHOT_REGENERATED" not in FATAL_STEPS


class TestSaveStepValidation:
    """save_step only accepts canonical step names."""

    def test_rejects_unknown_step(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        with pytest.raises(ValueError, match="unknown release step"):
            save_step(state_path, "NOT_A_REAL_STEP")

    def test_accepts_all_canonical_steps(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        for step in RELEASE_STEPS:
            save_step(state_path, step)
        loaded = load_release_state(state_path)
        assert loaded["completed_steps"] == list(RELEASE_STEPS)

    def test_save_step_failure_rejects_unknown_step(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        with pytest.raises(ValueError, match="unknown release step"):
            save_step_failure(state_path, "NOT_A_REAL_STEP", "boom")


class TestFailureMarkers:
    """save_step_failure records failure markers distinct from success markers."""

    def test_records_failure(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        save_step_failure(state_path, "DEPLOYED", "deploy to prod failed")
        loaded = load_release_state(state_path)
        assert loaded["failed_steps"] == {"DEPLOYED": "deploy to prod failed"}
        assert "DEPLOYED" not in loaded["completed_steps"]

    def test_success_clears_failure_marker(self, tmp_path):
        """A later success (e.g. on resume) replaces the failure marker."""
        state_path = str(tmp_path / "state.json")
        save_release_state(state_path, {"completed_steps": []})
        save_step_failure(state_path, "PIPELINES_PUBLISHED", "publish failed")
        save_step(state_path, "PIPELINES_PUBLISHED")
        loaded = load_release_state(state_path)
        assert "PIPELINES_PUBLISHED" in loaded["completed_steps"]
        assert "PIPELINES_PUBLISHED" not in loaded.get("failed_steps", {})

    def test_failure_removes_success_marker(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        save_release_state(
            state_path, {"completed_steps": ["DEPLOYED"]}
        )
        save_step_failure(state_path, "DEPLOYED", "boom")
        loaded = load_release_state(state_path)
        assert "DEPLOYED" not in loaded["completed_steps"]
        assert loaded["failed_steps"] == {"DEPLOYED": "boom"}

    def test_get_failed_steps(self):
        state = {"completed_steps": [], "failed_steps": {"DEPLOYED": "x"}}
        assert get_failed_steps(state) == {"DEPLOYED": "x"}
        assert get_failed_steps({"completed_steps": []}) == {}


class TestCompleteness:
    """is_state_complete / get_missing_steps derive from the canonical list."""

    def test_empty_state_missing_everything(self):
        state = {"completed_steps": []}
        assert get_missing_steps(state) == list(RELEASE_STEPS)
        assert not is_state_complete(state)

    def test_all_success_markers_is_complete(self):
        state = {"completed_steps": list(RELEASE_STEPS)}
        assert get_missing_steps(state) == []
        assert not has_fatal_failure(state)
        assert is_state_complete(state)

    def test_non_fatal_failure_marker_still_complete(self):
        """A deploy failure marker counts as 'marked' and is non-fatal."""
        completed = [s for s in RELEASE_STEPS if s != "DEPLOYED"]
        state = {
            "completed_steps": completed,
            "failed_steps": {"DEPLOYED": "deploy failed"},
        }
        assert get_missing_steps(state) == []
        assert not has_fatal_failure(state)
        assert is_state_complete(state)

    def test_fatal_failure_marker_blocks_completeness(self):
        completed = [s for s in RELEASE_STEPS if s != "PIPELINES_PUBLISHED"]
        state = {
            "completed_steps": completed,
            "failed_steps": {"PIPELINES_PUBLISHED": "publish failed"},
        }
        assert get_missing_steps(state) == []
        assert has_fatal_failure(state)
        assert not is_state_complete(state)

    def test_legacy_seven_step_state_is_incomplete(self):
        """A pre-overhaul state file with only mutating steps is incomplete."""
        state = {"completed_steps": list(MUTATING_STEPS)}
        assert get_missing_steps(state) == list(POST_RELEASE_STEPS)
        assert not is_state_complete(state)


class TestReleasableStatePath:
    """get_state_path/get_state_dir with a releasable dir relocate the state."""

    def test_standalone_unchanged(self):
        assert get_state_dir("/project") == "/project/.rlsbl/releases"
        assert get_state_path("/project") == (
            "/project/.rlsbl/releases/in-progress.json"
        )

    def test_releasable_dir_relocates(self):
        rel_dir = "/ws/.rlsbl-monorepo/releasables/alpha"
        assert get_state_dir("/ws/packages/core", releasable_dir=rel_dir) == (
            "/ws/.rlsbl-monorepo/releasables/alpha/releases"
        )
        assert get_state_path("/ws/packages/core", releasable_dir=rel_dir) == (
            "/ws/.rlsbl-monorepo/releasables/alpha/releases/in-progress.json"
        )

    def test_scrub_result_path(self):
        assert get_scrub_result_path("/project") == (
            "/project/.rlsbl/releases/scrub-result.json"
        )
        rel_dir = "/ws/.rlsbl-monorepo/releasables/alpha"
        assert get_scrub_result_path("/ws/packages/core", releasable_dir=rel_dir) == (
            "/ws/.rlsbl-monorepo/releasables/alpha/releases/scrub-result.json"
        )


class TestFindReleasableStateFiles:
    """find_releasable_state_files scans all releasables for in-progress state."""

    def _make_releasable(self, root, name, with_state=False):
        rel_dir = root / ".rlsbl-monorepo" / "releasables" / name
        rel_dir.mkdir(parents=True)
        (rel_dir / "version").write_text("1.0.0\n")
        if with_state:
            releases = rel_dir / "releases"
            releases.mkdir()
            (releases / "in-progress.json").write_text(
                json.dumps({"completed_steps": []}) + "\n"
            )
        return rel_dir

    def test_no_state_files(self, tmp_path):
        self._make_releasable(tmp_path, "alpha")
        self._make_releasable(tmp_path, "beta")
        assert find_releasable_state_files(str(tmp_path)) == []

    def test_one_state_file(self, tmp_path):
        self._make_releasable(tmp_path, "alpha", with_state=True)
        self._make_releasable(tmp_path, "beta")
        result = find_releasable_state_files(str(tmp_path))
        assert len(result) == 1
        name, path = result[0]
        assert name == "alpha"
        assert path.endswith(
            os.path.join(
                ".rlsbl-monorepo", "releasables", "alpha", "releases",
                "in-progress.json",
            )
        )

    def test_two_state_files_sorted(self, tmp_path):
        self._make_releasable(tmp_path, "beta", with_state=True)
        self._make_releasable(tmp_path, "alpha", with_state=True)
        result = find_releasable_state_files(str(tmp_path))
        assert [name for name, _ in result] == ["alpha", "beta"]

    def test_no_releasables_dir(self, tmp_path):
        assert find_releasable_state_files(str(tmp_path)) == []
