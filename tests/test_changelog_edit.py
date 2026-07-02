"""Tests for the changelog edit subcommand."""

import json
import os
import stat
from unittest import mock

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.files import (
    get_changes_dir,
    is_read_only,
)
from rlsbl.changelog.schema import parse_jsonl
from rlsbl.commands.changelog_cmd import cmd_add, cmd_edit


@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """Create a git repo with .rlsbl/ scaffolding and a baseline version tag."""
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

    # Create a baseline version tag so <tag>..HEAD works
    _run_git(repo, "tag", "v0.0.0")

    # Set up .rlsbl/changes with empty unreleased.jsonl
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")

    # config.json needed for cmd_add (reads batch_limits)
    (repo / ".rlsbl" / "config.json").write_text(json.dumps({"private": False}) + "\n")

    return repo


def _add_unreleased_entry(repo, sha, description="Feature", entry_type="feature", user_facing=True):
    """Add an entry to unreleased.jsonl using cmd_add."""
    flags = {
        "commits": sha,
        "description": description if user_facing else "",
        "type": entry_type if user_facing else "",
        "user-facing": user_facing,
        "auto-commit": False,
    }
    with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
        cmd_add(flags, project_root=repo)


def _create_released_jsonl(repo, version, entries):
    """Create a released (read-only) versioned JSONL file with given entries."""
    changes = repo / ".rlsbl" / "changes"
    jsonl_path = changes / f"{version}.jsonl"

    lines = []
    for entry in entries:
        data = {"commits": entry["commits"], "user_facing": entry.get("user_facing", False)}
        if entry.get("description"):
            data["description"] = entry["description"]
        if entry.get("type"):
            data["type"] = entry["type"]
        lines.append(json.dumps(data, separators=(",", ":")))

    jsonl_path.write_text("\n".join(lines) + "\n")
    os.chmod(str(jsonl_path), 0o444)
    return jsonl_path


class TestCmdEdit:
    """Tests for cmd_edit."""

    def test_edit_type(self, rlsbl_repo):
        """Edit an entry's type from feature to fix."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="My feature", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        cmd_edit(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(str(rlsbl_repo))
        entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert len(entries) == 1
        assert entries[0].type == "fix"
        # Description should be unchanged
        assert entries[0].description == "My feature"

    def test_edit_description(self, rlsbl_repo):
        """Edit an entry's description."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="Old description", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "",
            "description": "New description",
            "user-facing": None,
            "auto-commit": False,
        }
        cmd_edit(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(str(rlsbl_repo))
        entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert len(entries) == 1
        assert entries[0].description == "New description"
        assert entries[0].type == "feature"

    def test_edit_to_non_user_facing(self, rlsbl_repo):
        """Edit a user-facing entry to non-user-facing."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="Visible feature", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "",
            "description": "",
            "user-facing": False,
            "auto-commit": False,
        }
        cmd_edit(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(str(rlsbl_repo))
        entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert len(entries) == 1
        assert entries[0].user_facing is False
        assert entries[0].description is None
        assert entries[0].type is None

    def test_edit_to_user_facing(self, rlsbl_repo):
        """Edit a non-user-facing entry to user-facing with description and type."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, user_facing=False)

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "Now visible",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_edit(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(str(rlsbl_repo))
        entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert len(entries) == 1
        assert entries[0].user_facing is True
        assert entries[0].description == "Now visible"
        assert entries[0].type == "fix"

    def test_edit_not_found(self, rlsbl_repo):
        """Editing a commit hash not in any JSONL file errors."""
        sha = _make_commit(rlsbl_repo)
        # Don't add any entry for this commit

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_edit(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_edit_disambiguate_by_type(self, rlsbl_repo):
        """When two entries exist for the same commit with different types, --type disambiguates."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="A feature", entry_type="feature")
        _add_unreleased_entry(rlsbl_repo, sha, description="A fix", entry_type="fix")

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "Updated fix",
            "user-facing": None,
            "auto-commit": False,
        }
        cmd_edit(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(str(rlsbl_repo))
        entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert len(entries) == 2
        # The feature entry should be unchanged
        feature_entries = [e for e in entries if e.type == "fix"]
        assert len(feature_entries) == 1
        assert feature_entries[0].description == "Updated fix"
        # The other entry is unchanged
        other_entries = [e for e in entries if e.type == "feature"]
        assert len(other_entries) == 1
        assert other_entries[0].description == "A feature"

    def test_edit_ambiguous_without_type(self, rlsbl_repo):
        """Multiple entries for the same commit without --type errors."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="A feature", entry_type="feature")
        _add_unreleased_entry(rlsbl_repo, sha, description="A fix", entry_type="fix")

        flags = {
            "commits": sha,
            "type": "",
            "description": "Ambiguous edit",
            "user-facing": None,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_edit(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_edit_released_file_unlock_relock(self, rlsbl_repo):
        """Editing a released JSONL file temporarily unlocks it and re-locks after."""
        sha = _make_commit(rlsbl_repo)
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True, "description": "Original", "type": "feature"},
        ])
        assert is_read_only(str(jsonl_path))

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_edit(flags, project_root=rlsbl_repo)

        # File should be re-locked after edit
        assert is_read_only(str(jsonl_path))
        # Verify the edit took effect
        entries = parse_jsonl(str(jsonl_path))
        assert entries[0].type == "fix"

    def test_edit_released_file_changelog_regenerated(self, rlsbl_repo):
        """Editing a released entry regenerates CHANGELOG.md with the new description."""
        sha = _make_commit(rlsbl_repo)
        _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True, "description": "Old description", "type": "feature"},
        ])

        flags = {
            "commits": sha,
            "type": "",
            "description": "Brand new description",
            "user-facing": None,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_edit(flags, project_root=rlsbl_repo)

        changelog_path = rlsbl_repo / "CHANGELOG.md"
        assert changelog_path.exists()
        content = changelog_path.read_text()
        assert "Brand new description" in content
        assert "1.0.0" in content

    def test_edit_released_file_github_sync(self, rlsbl_repo):
        """Editing a released entry syncs GitHub Release notes."""
        sha = _make_commit(rlsbl_repo)
        _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True, "description": "Feature", "type": "feature"},
        ])

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release") as mock_sync:
                cmd_edit(flags, project_root=rlsbl_repo)

        mock_sync.assert_called_once_with("1.0.0")

    def test_edit_auto_commits(self, rlsbl_repo):
        """Editing with default auto-commit auto-commits the changed file."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="My feature", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            # auto-commit defaults to True
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_edit(flags, project_root=rlsbl_repo)

        mock_commit.assert_called_once()
        call_args = mock_commit.call_args
        commit_msg = call_args[0][0]
        files_list = call_args[0][1]
        assert "changelog: edit unreleased:" in commit_msg
        changes_dir = get_changes_dir(str(rlsbl_repo))
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        assert unreleased_path in files_list

    def test_edit_no_commit_flag(self, rlsbl_repo):
        """Editing with --no-auto-commit does not commit."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="My feature", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_edit(flags, project_root=rlsbl_repo)

        mock_commit.assert_not_called()

    def test_edit_multi_file_search(self, rlsbl_repo):
        """Edit finds entries across released and unreleased files."""
        sha_released = _make_commit(rlsbl_repo)
        sha_unreleased = _make_commit(rlsbl_repo, filename="other.txt")

        # Create a released file with one entry
        _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha_released], "user_facing": True, "description": "Released feature", "type": "feature"},
        ])

        # Add an unreleased entry for a different commit
        _add_unreleased_entry(rlsbl_repo, sha_unreleased, description="Unreleased feature", entry_type="feature")

        # Search for the commit in the released file
        flags = {
            "commits": sha_released,
            "type": "fix",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_edit(flags, project_root=rlsbl_repo)

        # Verify the released entry was edited
        changes_dir = get_changes_dir(str(rlsbl_repo))
        released_entries = parse_jsonl(os.path.join(changes_dir, "1.0.0.jsonl"))
        assert released_entries[0].type == "fix"
        assert released_entries[0].description == "Released feature"

        # Verify the unreleased entry was NOT touched
        unreleased_entries = parse_jsonl(os.path.join(changes_dir, "unreleased.jsonl"))
        assert unreleased_entries[0].type == "feature"
        assert unreleased_entries[0].description == "Unreleased feature"

    def test_edit_no_changes(self, rlsbl_repo):
        """Passing --commits without any edit flags errors."""
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="My feature", entry_type="feature")

        flags = {
            "commits": sha,
            "type": "",
            "description": "",
            "user-facing": None,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_edit(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1
