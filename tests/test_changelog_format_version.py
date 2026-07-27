"""Tests for the per-line format_version gate on JSONL changelog entries.

The strictspec-generated validators are the DOCUMENT authority for entry shape
(the per-line format_version gate plus the field/enum/conditional-required
shape). rlsbl serializes every new line with ``format_version = 1`` and reads in
an EXPLICIT two-mode fashion:

- a line carrying ``format_version`` is validated via strictspec (hard errors);
- a line lacking ``format_version`` is legacy and accepted ONLY when the caller
  opts into legacy mode (``enforce_format_version=False``, the transition
  default). With ``enforce_format_version=True`` a missing gate is a hard error.
"""

import json

import pytest

from rlsbl.changelog.schema import (
    CURRENT_FORMAT_VERSION,
    ChangelogEntry,
    parse_entry,
    parse_jsonl,
    serialize_entry,
    validate_schema,
)
from rlsbl.changelog.files import read_changelog_format_version_enforced
from rlsbl.errors import ChangelogError, ConfigError


class TestSerializeStampsFormatVersion:
    def test_serialize_emits_format_version(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        data = json.loads(serialize_entry(entry))
        assert data["format_version"] == CURRENT_FORMAT_VERSION

    def test_format_version_is_first_key(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        line = serialize_entry(entry)
        # format_version leads the line so the gate is cheap to read.
        assert line.startswith('{"format_version":1')

    def test_round_trip_stamped(self):
        original = ChangelogEntry(
            commits=["abc123"], user_facing=True, description="X", type="fix"
        )
        restored = parse_entry(serialize_entry(original))
        assert restored.commits == original.commits
        assert restored.description == original.description
        assert restored.type == original.type


class TestParseEntryGate:
    def test_stamped_line_ok(self):
        line = json.dumps({"format_version": 1, "commits": ["a"], "user_facing": False})
        entry = parse_entry(line)
        assert entry.commits == ["a"]

    def test_unsupported_format_version_rejected(self):
        line = json.dumps({"format_version": 2, "commits": ["a"], "user_facing": False})
        with pytest.raises(ChangelogError, match="format_version"):
            parse_entry(line)

    def test_legacy_line_accepted_by_default(self):
        """No format_version + legacy mode (default) is accepted."""
        line = json.dumps({"commits": ["a"], "user_facing": False})
        entry = parse_entry(line)
        assert entry.commits == ["a"]

    def test_legacy_line_rejected_when_enforced(self):
        line = json.dumps({"commits": ["a"], "user_facing": False})
        with pytest.raises(ChangelogError, match="format_version"):
            parse_entry(line, enforce_format_version=True)

    def test_stamped_line_ok_when_enforced(self):
        line = json.dumps({"format_version": 1, "commits": ["a"], "user_facing": False})
        entry = parse_entry(line, enforce_format_version=True)
        assert entry.user_facing is False

    def test_malformed_json_still_native_message(self):
        with pytest.raises(ChangelogError, match="malformed JSON"):
            parse_entry("{not json!!")


class TestParseJsonlEnforcement:
    def test_enforced_rejects_legacy_line_with_line_number(self, tmp_path):
        path = tmp_path / "c.jsonl"
        path.write_text(
            json.dumps({"format_version": 1, "commits": ["a"], "user_facing": False}) + "\n"
            + json.dumps({"commits": ["b"], "user_facing": False}) + "\n"
        )
        with pytest.raises(ChangelogError, match="line 2"):
            parse_jsonl(str(path), enforce_format_version=True)

    def test_enforced_accepts_all_stamped(self, tmp_path):
        path = tmp_path / "c.jsonl"
        path.write_text(
            json.dumps({"format_version": 1, "commits": ["a"], "user_facing": False}) + "\n"
            + json.dumps({"format_version": 1, "commits": ["b"], "user_facing": False}) + "\n"
        )
        entries = parse_jsonl(str(path), enforce_format_version=True)
        assert len(entries) == 2

    def test_legacy_default_accepts_unstamped(self, tmp_path):
        path = tmp_path / "c.jsonl"
        path.write_text(json.dumps({"commits": ["a"], "user_facing": False}) + "\n")
        assert len(parse_jsonl(str(path))) == 1


class TestValidateSchemaStillNative:
    """validate_schema keeps its native list[str] contract while routing the
    shape check through the strictspec validator."""

    def test_valid(self):
        entry = ChangelogEntry(commits=["a"], user_facing=True, description="X", type="fix")
        assert validate_schema(entry) == []

    def test_commits_empty(self):
        assert any("commits is empty" in e for e in validate_schema(ChangelogEntry(commits=[], user_facing=False)))

    def test_missing_description(self):
        errs = validate_schema(ChangelogEntry(commits=["a"], user_facing=True, type="fix"))
        assert any("missing description" in e for e in errs)

    def test_missing_type(self):
        errs = validate_schema(ChangelogEntry(commits=["a"], user_facing=True, description="X"))
        assert any("missing type" in e for e in errs)

    def test_invalid_type(self):
        errs = validate_schema(
            ChangelogEntry(commits=["a"], user_facing=True, description="X", type="performance")
        )
        assert any("invalid type" in e and "performance" in e for e in errs)

    def test_changeset_needs_id(self):
        errs = validate_schema(ChangelogEntry(user_facing=False), coverage_unit="changeset-file")
        assert any("id is required" in e for e in errs)

    def test_changeset_forbids_commits(self):
        errs = validate_schema(
            ChangelogEntry(commits=["a"], id="x", user_facing=False),
            coverage_unit="changeset-file",
        )
        assert any("commits must be empty" in e for e in errs)

    def test_unknown_coverage_unit(self):
        errs = validate_schema(ChangelogEntry(commits=["a"], user_facing=False), coverage_unit="bogus")
        assert any("unknown coverage_unit" in e for e in errs)


class TestEnforcementConfigReader:
    def test_absent_is_legacy_and_reports_absent(self):
        enforced, present = read_changelog_format_version_enforced({})
        assert enforced is False
        assert present is False

    def test_true(self):
        enforced, present = read_changelog_format_version_enforced(
            {"changelog_format_version_enforced": True}
        )
        assert enforced is True
        assert present is True

    def test_false(self):
        enforced, present = read_changelog_format_version_enforced(
            {"changelog_format_version_enforced": False}
        )
        assert enforced is False
        assert present is True

    def test_non_bool_is_hard_error(self):
        with pytest.raises(ConfigError):
            read_changelog_format_version_enforced(
                {"changelog_format_version_enforced": "yes"}
            )
