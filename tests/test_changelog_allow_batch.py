"""Tests for the --allow-batch flag in changelog add and related batch size hints."""

import json
import os

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.files import get_changes_dir, read_unreleased
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import check_batch_size_commits
from rlsbl.commands.changelog_cmd import cmd_add


@pytest.fixture
def batch_repo(tmp_path, monkeypatch):
    """Create a git repo with .rlsbl/ scaffolding and a low batch limit (max_commits_per_entry=3)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit
    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    # Baseline version tag
    _run_git(repo, "tag", "v0.0.0")

    # Set up .rlsbl/changes
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    # Set up config.json with max_commits_per_entry=3
    config = {
        "publish_mode": "ci",
        "batch_limits": {
            "max_commits_per_entry": 3,
            "max_entries_per_commit": 5,
            "exclusions": [],
        },
    }
    config_path = repo / ".rlsbl" / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    return repo


class TestAllowBatch:
    """Tests for the --allow-batch flag on changelog add."""

    def test_add_exceeds_batch_limit_fails_without_flag(self, batch_repo):
        """Adding an entry with more commits than the limit fails without --allow-batch."""
        # Create 5 commits (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(batch_repo, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=batch_repo)
        assert exc_info.value.code == 1

    def test_add_exceeds_batch_limit_succeeds_with_flag(self, batch_repo):
        """Adding an entry with --allow-batch succeeds and creates an exclusion in config.json."""
        # Create 5 commits (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(batch_repo, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": True,
        }
        cmd_add(flags, project_root=batch_repo)

        # Verify the entry was written
        entries = read_unreleased(get_changes_dir(str(batch_repo)))
        assert len(entries) == 1
        assert len(entries[0].commits) == 5
        assert entries[0].description == "Big batch feature"

        # Verify an exclusion was created in config.json
        config_path = batch_repo / ".rlsbl" / "config.json"
        config_data = json.loads(config_path.read_text())
        exclusions = config_data["batch_limits"]["exclusions"]
        assert len(exclusions) == 1

    def test_add_within_limit_succeeds_without_flag(self, batch_repo):
        """Adding an entry with fewer commits than the limit succeeds normally."""
        # Create 2 commits (under limit of 3)
        shas = []
        for i in range(2):
            shas.append(_make_commit(batch_repo, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Small feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": False,
        }
        cmd_add(flags, project_root=batch_repo)

        # Verify the entry was written successfully
        entries = read_unreleased(get_changes_dir(str(batch_repo)))
        assert len(entries) == 1
        assert len(entries[0].commits) == 2
        assert entries[0].description == "Small feature"

    def test_allow_batch_creates_correct_exclusion(self, batch_repo):
        """The exclusion created by --allow-batch has correct structure: reason, entries with version and line."""
        # Create 5 commits (exceeds limit of 3)
        shas = []
        for i in range(5):
            shas.append(_make_commit(batch_repo, f"file{i}.txt", f"change {i}"))

        flags = {
            "commits": ",".join(shas),
            "description": "Big batch feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
            "allow-batch": True,
        }
        cmd_add(flags, project_root=batch_repo)

        # Read the config and inspect the exclusion
        config_path = batch_repo / ".rlsbl" / "config.json"
        config_data = json.loads(config_path.read_text())
        exclusions = config_data["batch_limits"]["exclusions"]
        assert len(exclusions) == 1

        exclusion = exclusions[0]
        # Must have a reason field
        assert "reason" in exclusion
        assert isinstance(exclusion["reason"], str)
        assert len(exclusion["reason"]) > 0

        # Must have an entries array with version and line
        assert "entries" in exclusion
        assert len(exclusion["entries"]) == 1
        entry_ref = exclusion["entries"][0]
        assert entry_ref["version"] == "unreleased"
        # This is the first entry, so line number should be 1
        assert entry_ref["line"] == 1


class TestBatchCheckHintMessage:
    """Tests for the hint message in check_batch_size_commits."""

    def test_batch_check_hint_message(self):
        """check_batch_size_commits includes hint about exclusions and --allow-batch when entries exceed limit."""
        entries = [
            ChangelogEntry(
                commits=["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40],
                user_facing=True,
                description="Large change",
                type="feature",
            ),
        ]
        config = {
            "max_commits_per_entry": 3,
            "max_entries_per_commit": 5,
            "exclusions": [],
        }
        passed, details = check_batch_size_commits(entries, config, version="unreleased")
        assert passed is False
        assert len(details) >= 2
        # The last detail should be the hint message
        hint = details[-1]
        assert "exclusion" in hint.lower() or "exclusion" in hint
        assert "--allow-batch" in hint
        assert "batch_limits.exclusions" in hint
