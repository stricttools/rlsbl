"""Tests for the changelog amend subcommand."""

import json
import os
import stat
from unittest import mock

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.files import (
    append_entry_to_version,
    get_changes_dir,
    is_read_only,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.commands.changelog_cmd import cmd_amend


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

    # Set up .rlsbl/changes
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    return repo


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


class TestCmdAmend:
    """Tests for cmd_amend."""

    def test_amend_adds_entry_and_relocks(self, rlsbl_repo):
        """Amend adds an entry to a released JSONL file and re-locks it."""
        sha = _make_commit(rlsbl_repo)
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": False},
        ])
        assert is_read_only(str(jsonl_path))

        sha2 = _make_commit(rlsbl_repo, "b.txt")
        flags = {
            "version": "1.0.0",
            "commits": sha2[:12],
            "description": "New bugfix",
            "type": "fix",
            "no-user-facing": False,
            "no-resolve": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_amend(flags, project_root=rlsbl_repo)

        # Entry was appended
        entries = parse_jsonl(str(jsonl_path))
        assert len(entries) == 2
        assert entries[1].user_facing is True
        assert entries[1].description == "New bugfix"
        assert entries[1].type == "fix"
        assert len(entries[1].commits[0]) == 40

        # File is re-locked
        assert is_read_only(str(jsonl_path))

    def test_amend_no_resolve_skips_hash_validation(self, rlsbl_repo):
        """Amend with --no-resolve skips hash validation."""
        sha = _make_commit(rlsbl_repo)
        _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": False},
        ])

        flags = {
            "version": "1.0.0",
            "commits": "deadbeefdeadbeef",
            "description": "Old change",
            "type": "feature",
            "no-user-facing": False,
            "no-resolve": True,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_amend(flags, project_root=rlsbl_repo)

        changes_dir = get_changes_dir(".")
        entries = parse_jsonl(os.path.join(changes_dir, "1.0.0.jsonl"))
        assert len(entries) == 2
        assert entries[1].commits == ["deadbeefdeadbeef"]

    def test_amend_nonexistent_version_errors(self, rlsbl_repo):
        """Amend on a nonexistent version errors."""
        flags = {
            "version": "99.99.99",
            "commits": "abc123",
            "description": "Does not matter",
            "type": "fix",
            "no-user-facing": False,
            "no-resolve": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_amend(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_file_relocked_on_error(self, rlsbl_repo):
        """The file is re-locked even if an error occurs during append."""
        sha = _make_commit(rlsbl_repo)
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": False},
        ])
        assert is_read_only(str(jsonl_path))

        flags = {
            "version": "1.0.0",
            "commits": sha,
            "description": "Something",
            "type": "fix",
            "no-user-facing": False,
            "no-resolve": False,
        }

        with mock.patch(
            "rlsbl.commands.changelog_cmd.append_entry_to_version",
            side_effect=RuntimeError("simulated failure"),
        ):
            with pytest.raises(RuntimeError, match="simulated failure"):
                cmd_amend(flags, project_root=rlsbl_repo)

        # File must still be locked after error
        assert is_read_only(str(jsonl_path))

    def test_changelog_regenerated_after_amend(self, rlsbl_repo):
        """CHANGELOG.md is regenerated after amend."""
        sha = _make_commit(rlsbl_repo)
        _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True, "description": "Original feature", "type": "feature"},
        ])

        sha2 = _make_commit(rlsbl_repo, "c.txt")
        flags = {
            "version": "1.0.0",
            "commits": sha2,
            "description": "Added bugfix",
            "type": "fix",
            "no-user-facing": False,
            "no-resolve": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                cmd_amend(flags, project_root=rlsbl_repo)

        changelog_path = rlsbl_repo / "CHANGELOG.md"
        assert changelog_path.exists()
        content = changelog_path.read_text()
        assert "Original feature" in content
        assert "Added bugfix" in content
        assert "1.0.0" in content

    def test_file_relocked_after_duplicate_commit_error(self, rlsbl_repo):
        """File is re-locked when _check_duplicate_commits exits on duplicate."""
        sha = _make_commit(rlsbl_repo)
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True, "description": "Original", "type": "fix"},
        ])
        assert is_read_only(str(jsonl_path))

        # Try to amend with the SAME commit and same type/user_facing -- triggers hard error
        flags = {
            "version": "1.0.0",
            "commits": sha,
            "description": "Duplicate entry",
            "type": "fix",
            "no-user-facing": False,
            "no-resolve": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            with mock.patch("rlsbl.commands.changelog_cmd._sync_github_release"):
                with pytest.raises(SystemExit) as exc_info:
                    cmd_amend(flags, project_root=rlsbl_repo)
                assert exc_info.value.code == 1

        # File MUST be re-locked after the duplicate error
        assert is_read_only(str(jsonl_path))
