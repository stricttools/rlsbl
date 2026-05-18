"""Tests for rlsbl.changelog.generate."""

import json
import os

import pytest

from rlsbl.changelog.generate import (
    generate_changelog,
    generate_version_file,
    generate_version_section,
)
from rlsbl.changelog.schema import ChangelogEntry


def _jsonl_line(**kwargs) -> str:
    """Build one JSONL line from keyword args."""
    return json.dumps(kwargs, separators=(",", ":"))


class TestGenerateVersionSection:
    """Tests for generate_version_section."""

    def test_all_types_grouped_and_ordered(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Removed old API", type="breaking"),
            ChangelogEntry(commits=["b"], user_facing=True, description="Added widgets", type="feature"),
            ChangelogEntry(commits=["c"], user_facing=True, description="Fixed crash", type="fix"),
            ChangelogEntry(commits=["d"], user_facing=True, description="Another break", type="breaking"),
        ]
        md = generate_version_section("1.0.0", entries)

        # Verify heading
        assert md.startswith("## 1.0.0\n")

        # Verify group order: Breaking before Features before Fixes
        breaking_pos = md.index("### Breaking")
        features_pos = md.index("### Features")
        fixes_pos = md.index("### Fixes")
        assert breaking_pos < features_pos < fixes_pos

        # Verify entries under correct groups
        assert "- Removed old API" in md
        assert "- Another break" in md
        assert "- Added widgets" in md
        assert "- Fixed crash" in md

        # No Other section
        assert "### Other" not in md

    def test_no_user_facing_entries(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
            ChangelogEntry(commits=["b"], user_facing=False),
        ]
        md = generate_version_section("2.0.0", entries)
        assert "## 2.0.0" in md
        assert "- No user-facing changes." in md

    def test_empty_entries(self):
        md = generate_version_section("0.1.0", [])
        assert "## 0.1.0" in md
        assert "- No user-facing changes." in md

    def test_only_some_types_present(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A fix", type="fix"),
            ChangelogEntry(commits=["b"], user_facing=True, description="A feature", type="feature"),
        ]
        md = generate_version_section("3.0.0", entries)

        assert "### Features" in md
        assert "### Fixes" in md
        # Breaking and Other should not appear
        assert "### Breaking" not in md
        assert "### Other" not in md

    def test_unknown_type_goes_to_other(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Faster startup", type="performance"),
        ]
        md = generate_version_section("1.1.0", entries)
        assert "### Other" in md
        assert "- Faster startup" in md
        # Known type headers should not appear
        assert "### Breaking" not in md
        assert "### Features" not in md
        assert "### Fixes" not in md

    def test_none_type_goes_to_other(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Misc change", type=None),
        ]
        md = generate_version_section("1.2.0", entries)
        assert "### Other" in md
        assert "- Misc change" in md

    def test_mixed_user_facing_and_not(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Visible", type="feature"),
            ChangelogEntry(commits=["b"], user_facing=False),
            ChangelogEntry(commits=["c"], user_facing=True, description="Also visible", type="fix"),
        ]
        md = generate_version_section("4.0.0", entries)
        assert "- Visible" in md
        assert "- Also visible" in md
        # Non-user-facing entry has no description to check, but ensure
        # only 2 bullet items
        assert md.count("- ") == 2


class TestGenerateVersionFile:
    """Tests for generate_version_file."""

    def test_creates_md_file(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        jsonl = changes / "1.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="A feature", type="feature") + "\n"
        )

        md = generate_version_file(str(changes), "1.0.0")
        md_path = changes / "1.0.0.md"

        assert md_path.exists()
        content = md_path.read_text()
        assert content == md
        assert "## 1.0.0" in content
        assert "- A feature" in content

    def test_returns_markdown_text(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        jsonl = changes / "0.5.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["x"], user_facing=False) + "\n"
        )

        md = generate_version_file(str(changes), "0.5.0")
        assert "## 0.5.0" in md
        assert "No user-facing changes." in md


class TestGenerateChangelog:
    """Tests for generate_changelog."""

    def _setup_project(self, tmp_path, versions=None, unreleased_lines=None, config=None):
        """Helper to set up a project with .rlsbl/changes/ and optional config."""
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        if versions:
            for ver, lines in versions.items():
                jsonl = changes / f"{ver}.jsonl"
                jsonl.write_text("\n".join(lines) + "\n")

        if unreleased_lines:
            unreleased = changes / "unreleased.jsonl"
            unreleased.write_text("\n".join(unreleased_lines) + "\n")

        if config is not None:
            config_path = tmp_path / ".rlsbl" / "config.json"
            config_path.write_text(json.dumps(config))

        return tmp_path

    def test_full_generation_multiple_versions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Initial release", type="feature"),
                ],
                "2.0.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Breaking change", type="breaking"),
                ],
                "1.1.0": [
                    _jsonl_line(commits=["c"], user_facing=True, description="A fix", type="fix"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path))

        # Header present
        assert content.startswith("<!-- Generated by rlsbl from .rlsbl/changes/")
        assert "# Changelog" in content

        # All versions present
        assert "## 2.0.0" in content
        assert "## 1.1.0" in content
        assert "## 1.0.0" in content

        # Newest first
        pos_2 = content.index("## 2.0.0")
        pos_11 = content.index("## 1.1.0")
        pos_1 = content.index("## 1.0.0")
        assert pos_2 < pos_11 < pos_1

        # CHANGELOG.md written to project root
        changelog_path = tmp_path / "CHANGELOG.md"
        assert changelog_path.exists()
        assert changelog_path.read_text() == content

    def test_unreleased_section_at_top(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Feature", type="feature"),
                ],
            },
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="WIP feature", type="feature"),
            ],
        )

        content = generate_changelog(str(tmp_path))

        assert "## Unreleased" in content
        assert "- WIP feature" in content

        # Unreleased before versioned sections
        pos_unrel = content.index("## Unreleased")
        pos_1 = content.index("## 1.0.0")
        assert pos_unrel < pos_1

    def test_per_version_md_files_created(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature"),
                ],
                "2.0.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Break", type="breaking"),
                ],
            },
        )

        generate_changelog(str(tmp_path))

        changes = tmp_path / ".rlsbl" / "changes"
        assert (changes / "1.0.0.md").exists()
        assert (changes / "2.0.0.md").exists()

    def test_config_default_grouped(self, tmp_path, monkeypatch):
        """Default format (no config) works fine."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature"),
                ],
            },
        )

        # No config.json at all -- should default to grouped
        content = generate_changelog(str(tmp_path))
        assert "## 1.0.0" in content
        assert "### Features" in content

    def test_config_unrecognized_format_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature"),
                ],
            },
            config={"changelog_format": "flat"},
        )

        content = generate_changelog(str(tmp_path))

        # Warning on stderr
        captured = capsys.readouterr()
        assert "unrecognized changelog_format 'flat'" in captured.err

        # Still generates grouped output
        assert "## 1.0.0" in content
        assert "### Features" in content

    def test_empty_unreleased_not_included(self, tmp_path, monkeypatch):
        """Empty unreleased.jsonl should not produce an Unreleased section."""
        monkeypatch.chdir(tmp_path)
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        jsonl = changes / "1.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature") + "\n"
        )

        content = generate_changelog(str(tmp_path))
        assert "## Unreleased" not in content
        assert "## 1.0.0" in content

    def test_version_override_renames_unreleased_heading(self, tmp_path, monkeypatch):
        """version_override replaces the 'Unreleased' heading when entries exist."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Old feat", type="feature"),
                ],
            },
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="WIP feat", type="feature"),
            ],
        )

        content = generate_changelog(str(tmp_path), version_override="0.42.0")

        # Heading was renamed
        assert "## 0.42.0" in content
        assert "## Unreleased" not in content
        # Versioned section keeps its natural heading
        assert "## 1.0.0" in content
        # The unreleased entry's description appears under the new heading
        pos_new = content.index("## 0.42.0")
        pos_old = content.index("## 1.0.0")
        assert pos_new < pos_old
        assert content.index("- WIP feat") > pos_new
        assert content.index("- WIP feat") < pos_old

    def test_version_override_default_preserves_unreleased(self, tmp_path, monkeypatch):
        """Without version_override, the heading remains 'Unreleased'."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="WIP feat", type="feature"),
            ],
        )

        content = generate_changelog(str(tmp_path))
        assert "## Unreleased" in content
        assert "## 0.42.0" not in content

    def test_version_override_only_renames_unreleased_section(self, tmp_path, monkeypatch):
        """version_override does not affect existing versioned section headings."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Feat one", type="feature"),
                ],
                "2.0.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Break two", type="breaking"),
                ],
            },
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="WIP", type="feature"),
            ],
        )

        content = generate_changelog(str(tmp_path), version_override="3.0.0")

        # New heading replaces Unreleased
        assert "## 3.0.0" in content
        assert "## Unreleased" not in content
        # Existing versioned headings are untouched
        assert "## 2.0.0" in content
        assert "## 1.0.0" in content

    def test_version_override_with_no_unreleased_entries_is_noop(self, tmp_path, monkeypatch):
        """When unreleased.jsonl is empty, version_override has no effect."""
        monkeypatch.chdir(tmp_path)
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        jsonl = changes / "1.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature") + "\n"
        )

        content = generate_changelog(str(tmp_path), version_override="9.9.9")
        # No section heading for the override since there were no unreleased entries
        assert "## 9.9.9" not in content
        assert "## Unreleased" not in content
        assert "## 1.0.0" in content
