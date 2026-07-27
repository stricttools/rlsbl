"""Tests for the one-time format_version stamping bootstrap.

Covers the stamping tool (scripts/stamp_changelog_format_version.py) and the
MANDATORY pre-backfill guarantees that must hold before any fleet stamping:

(a) CHANGELOG regeneration is BYTE-IDENTICAL pre-stamp vs post-stamp -- the
    format_version must never leak into generated markdown.
(b) The .validated cache behaves honestly across stamping: rewriting
    unreleased.jsonl invalidates the cache (its mtime moves), while the
    verdict is otherwise unchanged because no check reads format_version.
(c) Finalized files are re-locked 444 after stamping and parse green in
    enforced mode.
"""

import importlib.util
import json
import os
import stat

import pytest

from rlsbl.changelog import validate as clv
from rlsbl.changelog.generate import generate_version_file
from rlsbl.changelog.schema import parse_jsonl
from rlsbl.errors import ChangelogError


# Import the stamping script as a module (it lives under scripts/, not the package).
_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "stamp_changelog_format_version.py",
)
_spec = importlib.util.spec_from_file_location("stamp_cfv", _SCRIPT)
stamp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp)


# Two legacy finalized lines: one compact, one spaced -- to prove the stamper
# preserves the ORIGINAL bytes and does not reshape.
_LEGACY_COMPACT = '{"commits":["a1b2c3"],"user_facing":true,"description":"**X.**","type":"fix"}'
_LEGACY_SPACED = '{"commits": ["d4e5f6"], "user_facing": false}'


def _is_ro(path):
    return not (os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class TestStampLine:
    def test_stamps_compact_line_additively(self):
        out = stamp.stamp_line(_LEGACY_COMPACT)
        assert out == '{"format_version":1,"commits":["a1b2c3"],"user_facing":true,"description":"**X.**","type":"fix"}'

    def test_stamps_spaced_line_without_reshaping(self):
        out = stamp.stamp_line(_LEGACY_SPACED)
        # Only format_version prepended; the original spacing is preserved.
        assert out == '{"format_version":1,"commits": ["d4e5f6"], "user_facing": false}'
        assert json.loads(out)["commits"] == ["d4e5f6"]

    def test_refuses_already_stamped_line(self):
        with pytest.raises(stamp.StampError, match="already stamped"):
            stamp.stamp_line('{"format_version":1,"commits":["a"],"user_facing":false}')

    def test_refuses_non_object(self):
        with pytest.raises(stamp.StampError):
            stamp.stamp_line("[1,2,3]")


class TestStampFile:
    def _write(self, path, lines, ro=False):
        path.write_text("\n".join(lines) + "\n")
        if ro:
            os.chmod(path, 0o444)

    def test_stamps_and_relocks_readonly(self, tmp_path):
        f = tmp_path / "0.1.0.jsonl"
        self._write(f, [_LEGACY_COMPACT, _LEGACY_SPACED], ro=True)
        assert _is_ro(str(f))

        n = stamp.stamp_file(str(f), dry_run=False)
        assert n == 2
        # Re-locked to 444 after stamping.
        assert _is_ro(str(f))
        entries = parse_jsonl(str(f), enforce_format_version=True)
        assert len(entries) == 2

    def test_keeps_writable_file_writable(self, tmp_path):
        f = tmp_path / "unreleased.jsonl"
        self._write(f, [_LEGACY_COMPACT])
        stamp.stamp_file(str(f), dry_run=False)
        assert not _is_ro(str(f))

    def test_dry_run_writes_nothing(self, tmp_path):
        f = tmp_path / "0.1.0.jsonl"
        self._write(f, [_LEGACY_COMPACT])
        before = f.read_text()
        n = stamp.stamp_file(str(f), dry_run=True)
        assert n == 1
        assert f.read_text() == before

    def test_refuses_already_stamped_file(self, tmp_path):
        f = tmp_path / "0.1.0.jsonl"
        self._write(f, [_LEGACY_COMPACT])
        stamp.stamp_file(str(f), dry_run=False)
        # Running again must refuse (no silent re-stamp).
        with pytest.raises(stamp.StampError, match="already stamped"):
            stamp.stamp_file(str(f), dry_run=False)

    def test_preserves_trailing_newline_and_blank_lines(self, tmp_path):
        f = tmp_path / "0.1.0.jsonl"
        f.write_text(_LEGACY_COMPACT + "\n\n" + _LEGACY_SPACED + "\n")
        stamp.stamp_file(str(f), dry_run=False)
        text = f.read_text()
        assert text.endswith("\n")
        # Blank line preserved between the two entries.
        assert "\n\n" in text
        assert len(parse_jsonl(str(f))) == 2


class TestPreBackfillGuarantees:
    """The mandatory pre-backfill test: byte-identity, cache honesty, 444 + enforced."""

    def _make_changes_dir(self, tmp_path):
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        finalized = changes / "0.1.0.jsonl"
        finalized.write_text(_LEGACY_COMPACT + "\n" + _LEGACY_SPACED + "\n")
        os.chmod(finalized, 0o444)
        return str(changes), str(finalized)

    def test_a_regeneration_is_byte_identical(self, tmp_path):
        changes_dir, _finalized = self._make_changes_dir(tmp_path)

        md_before = generate_version_file(changes_dir, "0.1.0", write_to_disk=False)
        stamp.stamp_changes_dir(changes_dir, dry_run=False)
        md_after = generate_version_file(changes_dir, "0.1.0", write_to_disk=False)

        # format_version must NOT leak into generated markdown.
        assert md_after == md_before
        assert "format_version" not in md_after

    def test_c_finalized_relocked_444_and_parses_enforced(self, tmp_path):
        changes_dir, finalized = self._make_changes_dir(tmp_path)
        stamp.stamp_changes_dir(changes_dir, dry_run=False)

        assert _is_ro(finalized)
        entries = parse_jsonl(finalized, enforce_format_version=True)
        assert len(entries) == 2

    def test_b_stamping_unreleased_invalidates_cache(self, tmp_path, monkeypatch):
        changes_dir, _finalized = self._make_changes_dir(tmp_path)
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w", encoding="utf-8") as f:
            f.write(_LEGACY_COMPACT + "\n")

        head = "a" * 40
        monkeypatch.setattr(clv, "_git_head", lambda: head)
        monkeypatch.setattr(clv, "_is_ancestor", lambda a, b: True)

        # Seed a valid cache: write .validated AFTER unreleased so its mtime is newer.
        cache = os.path.join(changes_dir, ".validated")
        with open(cache, "w", encoding="utf-8") as f:
            f.write(head + "\n")
        # Make the cache strictly newer than unreleased.jsonl.
        old = os.path.getmtime(unreleased) - 10
        os.utime(unreleased, (old, old))
        assert clv._is_cache_valid(changes_dir) is True

        # Stamping rewrites unreleased.jsonl -> its mtime moves forward -> the
        # cache is honestly invalidated (a stamped file is a changed file).
        stamp.stamp_changes_dir(changes_dir, dry_run=False)
        assert clv._is_cache_valid(changes_dir) is False

    def test_b_stamping_only_finalized_leaves_cache_valid(self, tmp_path, monkeypatch):
        """Stamping finalized files (not unreleased) leaves the cache valid: the
        verdict is unchanged because no check reads format_version, and the cache
        keys on unreleased.jsonl's mtime only."""
        changes_dir, _finalized = self._make_changes_dir(tmp_path)
        unreleased = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased, "w", encoding="utf-8") as f:
            f.write("")  # empty unreleased

        head = "b" * 40
        monkeypatch.setattr(clv, "_git_head", lambda: head)
        monkeypatch.setattr(clv, "_is_ancestor", lambda a, b: True)

        cache = os.path.join(changes_dir, ".validated")
        with open(cache, "w", encoding="utf-8") as f:
            f.write(head + "\n")
        old = os.path.getmtime(unreleased) - 10
        os.utime(unreleased, (old, old))
        assert clv._is_cache_valid(changes_dir) is True

        # Only the finalized file is rewritten; unreleased.jsonl is untouched.
        stamp.stamp_file(_finalized_of(changes_dir), dry_run=False)
        assert clv._is_cache_valid(changes_dir) is True


def _finalized_of(changes_dir):
    return os.path.join(changes_dir, "0.1.0.jsonl")
