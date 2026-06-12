"""Tests for rlsbl.changelog.files."""

import json
import os
import stat

import pytest

from conftest import run_git as _run_git, git_head as _git_head, make_commit as _make_commit
from rlsbl.changelog.files import (
    append_entry,
    changes_dir_exists,
    finalize_version,
    get_changes_dir,
    is_read_only,
    list_versioned_files,
    read_unreleased,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.errors import ChangelogError


class TestGetChangesDir:
    """Tests for get_changes_dir."""

    def test_returns_expected_path(self, tmp_path):
        result = get_changes_dir(str(tmp_path))
        assert result == os.path.join(str(tmp_path), ".rlsbl", "changes")


class TestChangesDirExists:
    """Tests for changes_dir_exists."""

    def test_returns_false_when_missing(self, tmp_path):
        assert changes_dir_exists(str(tmp_path)) is False

    def test_returns_true_when_present(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        assert changes_dir_exists(str(tmp_path)) is True


class TestListVersionedFiles:
    """Tests for list_versioned_files."""

    def test_empty_directory(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        assert list_versioned_files(str(changes)) == []

    def test_nonexistent_directory(self, tmp_path):
        assert list_versioned_files(str(tmp_path / "nope")) == []

    def test_sorts_by_semver_descending(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        # Create files in non-sorted order
        for name in ["0.1.0.jsonl", "1.0.0.jsonl", "0.2.0.jsonl", "0.1.1.jsonl", "2.0.0.jsonl"]:
            (changes / name).write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert versions == ["2.0.0", "1.0.0", "0.2.0", "0.1.1", "0.1.0"]

    def test_ignores_non_versioned_files(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.0.0.jsonl").write_text("")
        (changes / "unreleased.jsonl").write_text("")
        (changes / "notes.txt").write_text("")
        (changes / ".validated").write_text("")

        result = list_versioned_files(str(changes))
        assert len(result) == 1
        assert result[0][0] == "1.0.0"

    def test_returns_full_paths(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.2.3.jsonl").write_text("")

        result = list_versioned_files(str(changes))
        assert result[0][1] == str(changes / "1.2.3.jsonl")


class TestReadUnreleased:
    """Tests for read_unreleased."""

    def test_returns_empty_when_file_missing(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        assert read_unreleased(str(changes)) == []

    def test_reads_entries(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        path = changes / "unreleased.jsonl"
        lines = [
            json.dumps({"commits": ["a"], "user_facing": False}),
            json.dumps({"commits": ["b"], "user_facing": True, "description": "X", "type": "fix"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        entries = read_unreleased(str(changes))
        assert len(entries) == 2
        assert entries[0].commits == ["a"]
        assert entries[1].type == "fix"


class TestAppendEntry:
    """Tests for append_entry."""

    def test_creates_dir_and_file(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        append_entry(changes, entry)

        assert os.path.isdir(changes)
        entries = read_unreleased(changes)
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

    def test_appends_to_existing(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry1 = ChangelogEntry(commits=["abc"], user_facing=False)
        entry2 = ChangelogEntry(
            commits=["def"],
            user_facing=True,
            description="Feature",
            type="feature",
        )
        append_entry(changes, entry1)
        append_entry(changes, entry2)

        entries = read_unreleased(changes)
        assert len(entries) == 2
        assert entries[0].commits == ["abc"]
        assert entries[1].description == "Feature"

    def test_no_temp_files_left(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        append_entry(changes, entry)

        files = os.listdir(changes)
        assert all(not f.endswith(".tmp") for f in files)


class TestFinalizeVersion:
    """Tests for finalize_version."""

    def test_renames_and_creates_new_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "1.0.0")

        versioned = changes / "1.0.0.jsonl"
        assert versioned.exists()
        assert not unreleased.read_text().strip()  # new empty file
        # Original content moved to versioned file
        entries = parse_jsonl(str(versioned))
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

    def test_versioned_file_is_read_only(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "2.0.0")

        versioned = changes / "2.0.0.jsonl"
        mode = os.stat(str(versioned)).st_mode
        assert not (mode & stat.S_IWUSR)
        assert not (mode & stat.S_IWGRP)
        assert not (mode & stat.S_IWOTH)

    def test_raises_when_no_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        with pytest.raises(FileNotFoundError, match="unreleased.jsonl not found"):
            finalize_version(str(changes), "1.0.0")

    def test_refuses_to_overwrite_existing_versioned_file(self, tmp_path):
        """A re-run after a mid-release failure must not clobber an
        already-finalized (read-only) changelog file."""
        changes = tmp_path / "changes"
        changes.mkdir()

        existing_content = (
            json.dumps({"commits": ["old1"], "user_facing": False}) + "\n"
        )
        existing = changes / "1.2.3.jsonl"
        existing.write_text(existing_content)
        os.chmod(str(existing), 0o444)

        unreleased_content = (
            json.dumps({"commits": ["new1"], "user_facing": False}) + "\n"
        )
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(unreleased_content)

        with pytest.raises(ChangelogError, match=r"1\.2\.3\.jsonl"):
            finalize_version(str(changes), "1.2.3")

        # The already-finalized file is untouched (content and read-only mode)
        assert existing.read_text() == existing_content
        assert not (os.stat(str(existing)).st_mode & stat.S_IWUSR)
        # unreleased.jsonl still exists with its original content
        assert unreleased.read_text() == unreleased_content


class TestFinalizeVersionStaleWarning:
    """Tests for finalize_version's stale-entry warning (monorepo mode)."""

    @pytest.fixture
    def monorepo_repo(self, tmp_path, monkeypatch):
        """Git repo with one pre-tag commit, a monorepo tag, and a post-tag commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")

        pre_tag_sha = _git_head(repo)

        _run_git(repo, "tag", "mylib@v0.1.0")

        post_tag_sha = _make_commit(repo, "post.txt", "post-tag commit")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        return repo, pre_tag_sha, post_tag_sha

    def test_finalize_no_warnings_when_all_in_range(self, monorepo_repo, capsys):
        """All entries reference in-range commits => no warnings."""
        repo, _pre_tag_sha, post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": [post_tag_sha], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.2.0", tag_glob="mylib@v*")

        captured = capsys.readouterr()
        assert "warning" not in captured.err
        # Rename still happened.
        assert (changes / "0.2.0.jsonl").exists()
        assert unreleased.exists()
        assert unreleased.read_text() == ""

    def test_finalize_warns_on_stale_entries(self, monorepo_repo, capsys):
        """Entry referencing a pre-tag commit => warning printed, rename still happens."""
        repo, pre_tag_sha, post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            "\n".join(
                [
                    json.dumps({"commits": [post_tag_sha], "user_facing": False}),
                    json.dumps({"commits": [pre_tag_sha], "user_facing": False}),
                ]
            )
            + "\n"
        )

        finalize_version(str(changes), "0.2.0", tag_glob="mylib@v*")

        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert "line 2" in captured.err
        assert pre_tag_sha in captured.err
        # Line 1 references an in-range commit => not mentioned.
        assert "line 1" not in captured.err
        # Rename still happened.
        assert (changes / "0.2.0.jsonl").exists()
        assert unreleased.read_text() == ""

    def test_finalize_no_warnings_without_tag_glob(self, monorepo_repo, capsys):
        """Without tag_glob (non-monorepo case), no stale check runs."""
        repo, pre_tag_sha, _post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        # Even an entry with a pre-tag commit should not trigger a warning
        # because we didn't pass tag_glob.
        unreleased.write_text(
            json.dumps({"commits": [pre_tag_sha], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.2.0")

        captured = capsys.readouterr()
        assert "warning" not in captured.err
        assert (changes / "0.2.0.jsonl").exists()


class TestIsReadOnly:
    """Tests for is_read_only."""

    def test_nonexistent_file(self, tmp_path):
        assert is_read_only(str(tmp_path / "nope")) is False

    def test_writable_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert is_read_only(str(f)) is False

    def test_read_only_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        os.chmod(str(f), 0o444)
        assert is_read_only(str(f)) is True
