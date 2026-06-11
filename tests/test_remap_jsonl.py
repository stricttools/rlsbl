"""Tests for writable_jsonl context manager and remap_jsonl_hashes function."""

import json
import os
import stat

from rlsbl.changelog.files import (
    RemapResult,
    is_read_only,
    remap_jsonl_hashes,
    writable_jsonl,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry


def _write_entries(path, entries):
    """Write a list of ChangelogEntry objects to a JSONL file."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(serialize_entry(entry) + "\n")


class TestWritableJsonl:
    """Tests for writable_jsonl context manager."""

    def test_unlocks_read_only_file(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("")
        os.chmod(str(f), 0o444)
        assert is_read_only(str(f))

        with writable_jsonl(str(f)):
            assert not is_read_only(str(f))

        assert is_read_only(str(f))

    def test_leaves_writable_file_unchanged(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("")
        assert not is_read_only(str(f))

        with writable_jsonl(str(f)):
            assert not is_read_only(str(f))

        assert not is_read_only(str(f))

    def test_relocks_on_exception(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("")
        os.chmod(str(f), 0o444)

        try:
            with writable_jsonl(str(f)):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert is_read_only(str(f))


class TestRemapReplacesHashesInUnreleased:
    """Test that remap replaces hashes in unreleased.jsonl."""

    def test_remap_replaces_hashes_in_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"

        entries = [
            ChangelogEntry(commits=["aaa111"], user_facing=False),
            ChangelogEntry(
                commits=["bbb222"],
                user_facing=True,
                description="A feature",
                type="feature",
            ),
        ]
        _write_entries(str(unreleased), entries)

        sha_map = {"aaa111": "new_aaa", "bbb222": "new_bbb"}
        results = remap_jsonl_hashes(str(changes), sha_map)

        assert len(results) == 1
        result = results[0]
        assert result.path == str(unreleased)
        assert result.entries_modified == 2
        assert result.hashes_remapped == 2

        # Verify file contents
        updated = parse_jsonl(str(unreleased))
        assert updated[0].commits == ["new_aaa"]
        assert updated[1].commits == ["new_bbb"]
        assert updated[1].description == "A feature"
        assert updated[1].type == "feature"


class TestRemapReplacesHashesInVersioned:
    """Test that remap handles read-only versioned files."""

    def test_remap_replaces_hashes_in_versioned(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        versioned = changes / "1.0.0.jsonl"

        entries = [
            ChangelogEntry(
                commits=["old_hash"],
                user_facing=True,
                description="Fix bug",
                type="fix",
            ),
        ]
        _write_entries(str(versioned), entries)
        os.chmod(str(versioned), 0o444)
        assert is_read_only(str(versioned))

        sha_map = {"old_hash": "new_hash"}
        results = remap_jsonl_hashes(str(changes), sha_map)

        assert len(results) == 1
        assert results[0].entries_modified == 1
        assert results[0].hashes_remapped == 1

        # File should be re-locked after remap
        assert is_read_only(str(versioned))

        # Verify contents (need to unlock to read, but parse_jsonl can read read-only)
        updated = parse_jsonl(str(versioned))
        assert updated[0].commits == ["new_hash"]
        assert updated[0].description == "Fix bug"


class TestRemapSkipsUnaffectedFiles:
    """Test that files with no matching hashes are untouched."""

    def test_remap_skips_unaffected_files(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"

        entries = [
            ChangelogEntry(commits=["abc123"], user_facing=False),
        ]
        _write_entries(str(unreleased), entries)

        # Map contains hashes that don't exist in the file
        sha_map = {"xyz789": "new_xyz"}
        results = remap_jsonl_hashes(str(changes), sha_map)

        assert results == []

        # File unchanged
        updated = parse_jsonl(str(unreleased))
        assert updated[0].commits == ["abc123"]


class TestRemapHandlesMissingDir:
    """Test that a missing changes_dir returns empty results."""

    def test_remap_handles_missing_dir(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        results = remap_jsonl_hashes(str(nonexistent), {"a": "b"})
        assert results == []


class TestRemapPartialMapping:
    """Test that only matching hashes are replaced within an entry."""

    def test_remap_partial_mapping(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"

        entries = [
            ChangelogEntry(
                commits=["hash_a", "hash_b", "hash_c"],
                user_facing=True,
                description="Multi-commit feature",
                type="feature",
            ),
        ]
        _write_entries(str(unreleased), entries)

        # Only hash_b is in the map
        sha_map = {"hash_b": "new_hash_b"}
        results = remap_jsonl_hashes(str(changes), sha_map)

        assert len(results) == 1
        assert results[0].entries_modified == 1
        assert results[0].hashes_remapped == 1

        updated = parse_jsonl(str(unreleased))
        assert updated[0].commits == ["hash_a", "new_hash_b", "hash_c"]
