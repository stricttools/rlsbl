"""Tests for rlsbl.changelog.schema."""

import json
import os

import pytest

from rlsbl.changelog.schema import (
    ChangelogEntry,
    entry_content_key,
    parse_entry,
    parse_jsonl,
    serialize_entry,
    validate_schema,
)
from rlsbl.errors import ChangelogError


class TestChangelogEntry:
    """Basic dataclass tests."""

    def test_defaults(self):
        entry = ChangelogEntry()
        assert entry.commits == []
        assert entry.user_facing is False
        assert entry.description is None
        assert entry.type is None

    def test_with_values(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Added foo",
            type="feature",
        )
        assert entry.commits == ["abc123"]
        assert entry.user_facing is True
        assert entry.description == "Added foo"
        assert entry.type == "feature"


class TestValidateSchema:
    """Tests for validate_schema."""

    def test_valid_user_facing(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Fixed the bug",
            type="fix",
        )
        assert validate_schema(entry) == []

    def test_valid_non_user_facing(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=False,
        )
        assert validate_schema(entry) == []

    def test_empty_commits(self):
        entry = ChangelogEntry(commits=[], user_facing=False)
        errors = validate_schema(entry)
        assert "commits is empty" in errors

    def test_user_facing_missing_description(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            type="feature",
        )
        errors = validate_schema(entry)
        assert "user_facing entry missing description" in errors
        assert "user_facing entry missing type" not in errors

    def test_user_facing_missing_type(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Something",
        )
        errors = validate_schema(entry)
        assert "user_facing entry missing type" in errors
        assert "user_facing entry missing description" not in errors

    def test_user_facing_missing_both(self):
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
        )
        errors = validate_schema(entry)
        assert "user_facing entry missing description" in errors
        assert "user_facing entry missing type" in errors

    def test_non_user_facing_ignores_description_type(self):
        """Non-user-facing entries don't need description or type."""
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=False,
            description=None,
            type=None,
        )
        assert validate_schema(entry) == []

    @pytest.mark.parametrize("valid_type", ["feature", "fix", "breaking"])
    def test_valid_types_pass(self, valid_type):
        """User-facing entry with each valid type passes validation."""
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Added widget",
            type=valid_type,
        )
        assert validate_schema(entry) == []

    def test_invalid_type_rejected(self):
        """User-facing entry with a non-enum type is a schema error naming the value."""
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Faster startup",
            type="performance",
        )
        errors = validate_schema(entry)
        assert any("invalid type" in e and "performance" in e for e in errors)

    def test_invalid_type_non_user_facing_ignored(self):
        """Non-user-facing entries carry no type, so type enum is not enforced."""
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=False,
        )
        assert validate_schema(entry) == []

    def test_user_facing_none_type_still_fails(self):
        """User-facing entry with type=None fails with 'missing type'."""
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Something",
            type=None,
        )
        errors = validate_schema(entry)
        assert any("missing type" in e for e in errors)


class TestParseEntry:
    """Tests for parse_entry."""

    def test_valid_minimal(self):
        line = '{"commits":["abc"],"user_facing":false}'
        entry = parse_entry(line)
        assert entry.commits == ["abc"]
        assert entry.user_facing is False
        assert entry.description is None
        assert entry.type is None

    def test_valid_full(self):
        line = json.dumps({
            "commits": ["abc", "def"],
            "user_facing": True,
            "description": "New feature",
            "type": "feature",
        })
        entry = parse_entry(line)
        assert entry.commits == ["abc", "def"]
        assert entry.user_facing is True
        assert entry.description == "New feature"
        assert entry.type == "feature"

    def test_malformed_json(self):
        with pytest.raises(ChangelogError, match="malformed JSON"):
            parse_entry("{not json!!")

    def test_not_an_object(self):
        with pytest.raises(ChangelogError, match="must be a JSON object"):
            parse_entry("[1, 2, 3]")

    def test_missing_commits_defaults_to_empty(self):
        """Entries without commits parse with an empty commits list."""
        entry = parse_entry('{"user_facing": false}')
        assert entry.commits == []

    def test_missing_user_facing(self):
        with pytest.raises(ChangelogError, match="missing required field: user_facing"):
            parse_entry('{"commits": ["abc"]}')

    def test_commits_not_a_list(self):
        with pytest.raises(ChangelogError, match="commits must be a list"):
            parse_entry('{"commits": "abc", "user_facing": false}')


class TestSerializeEntry:
    """Tests for serialize_entry."""

    def test_minimal(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        line = serialize_entry(entry)
        assert "\n" not in line
        data = json.loads(line)
        # Every serialized line carries the per-line format_version gate.
        assert data == {"format_version": 1, "commits": ["abc"], "user_facing": False}

    def test_full(self):
        entry = ChangelogEntry(
            commits=["abc", "def"],
            user_facing=True,
            description="Fixed it",
            type="fix",
        )
        line = serialize_entry(entry)
        data = json.loads(line)
        assert data == {
            "format_version": 1,
            "commits": ["abc", "def"],
            "user_facing": True,
            "description": "Fixed it",
            "type": "fix",
        }

    def test_omits_none_fields(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        line = serialize_entry(entry)
        data = json.loads(line)
        assert "description" not in data
        assert "type" not in data

    def test_round_trip(self):
        original = ChangelogEntry(
            commits=["abc123", "def456"],
            user_facing=True,
            description="Added a feature",
            type="feature",
        )
        line = serialize_entry(original)
        restored = parse_entry(line)
        assert restored.commits == original.commits
        assert restored.user_facing == original.user_facing
        assert restored.description == original.description
        assert restored.type == original.type

    def test_round_trip_minimal(self):
        original = ChangelogEntry(commits=["abc"], user_facing=False)
        line = serialize_entry(original)
        restored = parse_entry(line)
        assert restored.commits == original.commits
        assert restored.user_facing == original.user_facing
        assert restored.description is None
        assert restored.type is None


class TestParseJsonl:
    """Tests for parse_jsonl."""

    def test_valid_file(self, tmp_path):
        path = tmp_path / "test.jsonl"
        lines = [
            json.dumps({"commits": ["a"], "user_facing": False}),
            json.dumps({"commits": ["b"], "user_facing": True, "description": "X", "type": "fix"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        entries = parse_jsonl(str(path))
        assert len(entries) == 2
        assert entries[0].commits == ["a"]
        assert entries[1].description == "X"

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "test.jsonl"
        content = (
            json.dumps({"commits": ["a"], "user_facing": False}) + "\n"
            + "\n"
            + json.dumps({"commits": ["b"], "user_facing": False}) + "\n"
        )
        path.write_text(content)
        entries = parse_jsonl(str(path))
        assert len(entries) == 2

    def test_malformed_line_reports_line_number(self, tmp_path):
        path = tmp_path / "test.jsonl"
        content = (
            json.dumps({"commits": ["a"], "user_facing": False}) + "\n"
            + "{bad json}\n"
        )
        path.write_text(content)
        with pytest.raises(ChangelogError, match="line 2"):
            parse_jsonl(str(path))

    def test_empty_file(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.write_text("")
        entries = parse_jsonl(str(path))
        assert entries == []


class TestEntryContentKey:
    """The identity of an entry that carries no ``id``.

    ``id`` is optional on read, so a historical entry has none and can only be
    identified by what it says. The key exists so a copy of an entry can be
    recognized as a copy -- and so two entries that say different things are
    never mistaken for one.
    """

    def _entry(self, **kwargs):
        base = dict(
            commits=["a1b2c3"], user_facing=True,
            description="Something", type="fix",
        )
        base.update(kwargs)
        return ChangelogEntry(**base)

    def test_two_entries_saying_the_same_thing_share_a_key(self):
        assert entry_content_key(self._entry()) == entry_content_key(self._entry())

    def test_commit_order_is_not_part_of_the_identity(self):
        one = self._entry(commits=["a1b2c3", "d4e5f6"])
        other = self._entry(commits=["d4e5f6", "a1b2c3"])
        assert entry_content_key(one) == entry_content_key(other)

    @pytest.mark.parametrize("field,value", [
        ("commits", ["999999"]),
        ("user_facing", False),
        ("description", "Something else"),
        ("type", "feature"),
        ("release_type", "ota"),
    ])
    def test_every_field_that_carries_meaning_separates_two_entries(
        self, field, value,
    ):
        assert entry_content_key(self._entry()) != entry_content_key(
            self._entry(**{field: value})
        )

    def test_the_id_is_not_part_of_the_key(self):
        """The key is the FALLBACK for entries that have no id."""
        assert entry_content_key(self._entry(id="x" * 48)) == entry_content_key(
            self._entry()
        )
