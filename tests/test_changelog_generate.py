"""Tests for rlsbl.changelog.generate."""

import json
import os

import pytest

from rlsbl.changelog.generate import (
    _base_version,
    _deduplicate_entries,
    _is_prerelease,
    _read_release_metadata,
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

    def test_description_added_after_heading(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A feature", type="feature"),
        ]
        md = generate_version_section("5.0.0", entries, description="Major overhaul of the widget system")
        assert "## 5.0.0" in md
        assert "Major overhaul of the widget system" in md
        # Description comes before the first type group
        desc_pos = md.index("Major overhaul of the widget system")
        features_pos = md.index("### Features")
        assert desc_pos < features_pos

    def test_context_added_as_details_block(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A fix", type="fix"),
        ]
        md = generate_version_section("5.1.0", entries, context="Users reported crashes on startup")
        assert "<details>" in md
        assert "<summary>Context</summary>" in md
        assert "Users reported crashes on startup" in md
        assert "</details>" in md

    def test_description_and_context_together(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="New API", type="feature"),
        ]
        md = generate_version_section(
            "6.0.0", entries,
            description="Redesigned widget API",
            context="The old API had performance issues",
        )
        assert "Redesigned widget API" in md
        assert "The old API had performance issues" in md
        # Description before context before entries
        desc_pos = md.index("Redesigned widget API")
        ctx_pos = md.index("<details>")
        feat_pos = md.index("### Features")
        assert desc_pos < ctx_pos < feat_pos

    def test_description_with_no_user_facing_entries(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section("7.0.0", entries, description="Internal refactor")
        assert "Internal refactor" in md
        assert "- No user-facing changes." in md

    def test_context_with_no_user_facing_entries(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=False),
        ]
        md = generate_version_section(
            "7.1.0", entries, context="Preparing for next major release",
        )
        assert "<details>" in md
        assert "Preparing for next major release" in md
        assert "- No user-facing changes." in md

    def test_empty_description_not_added(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Feat", type="feature"),
        ]
        md = generate_version_section("8.0.0", entries, description="")
        lines = md.strip().splitlines()
        # No empty paragraph between heading and ### Features
        assert lines[0] == "## 8.0.0"
        assert lines[1] == ""
        assert lines[2] == "### Features"

    def test_empty_context_not_added(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="Feat", type="feature"),
        ]
        md = generate_version_section("8.1.0", entries, context="")
        assert "<details>" not in md


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

    def test_description_flows_into_unreleased_section(self, tmp_path, monkeypatch):
        """description parameter appears in the unreleased section."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="New thing", type="feature"),
            ],
        )

        content = generate_changelog(
            str(tmp_path),
            version_override="2.0.0",
            description="Complete API redesign",
        )
        assert "Complete API redesign" in content
        # Description is in the 2.0.0 section
        desc_pos = content.index("Complete API redesign")
        heading_pos = content.index("## 2.0.0")
        assert desc_pos > heading_pos

    def test_context_flows_into_unreleased_section(self, tmp_path, monkeypatch):
        """context parameter appears as details block in unreleased section."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="Fix", type="fix"),
            ],
        )

        content = generate_changelog(
            str(tmp_path),
            version_override="1.1.0",
            context="Root cause was a race condition",
        )
        assert "<details>" in content
        assert "Root cause was a race condition" in content

    def test_description_not_applied_to_versioned_sections(self, tmp_path, monkeypatch):
        """description only affects the unreleased section, not existing versions."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Old feat", type="feature"),
                ],
            },
            unreleased_lines=[
                _jsonl_line(commits=["x"], user_facing=True, description="New feat", type="feature"),
            ],
        )

        content = generate_changelog(
            str(tmp_path),
            version_override="2.0.0",
            description="Only for the new version",
        )
        # Description appears after 2.0.0 heading
        desc_pos = content.index("Only for the new version")
        v2_pos = content.index("## 2.0.0")
        v1_pos = content.index("## 1.0.0")
        assert v2_pos < desc_pos < v1_pos

    def test_versioned_sections_include_archived_description_and_context(
        self, tmp_path, monkeypatch,
    ):
        """Description and context from archived .rlsbl/releases/v*.toml files
        are included in versioned sections on regeneration."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Initial", type="feature"),
                ],
                "2.0.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Big change", type="breaking"),
                ],
            },
        )

        # Create archived release toml files
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "v1.0.0.toml").write_text(
            'bump = "major"\ndescription = "First stable release"\ncontext = ""\n'
            'include = ["pypi"]\nexclude = []\n'
        )
        (releases_dir / "v2.0.0.toml").write_text(
            'bump = "major"\ndescription = "Major rewrite"\n'
            'context = "Migrated from REST to GraphQL"\n'
            'include = ["pypi"]\nexclude = []\n'
        )

        content = generate_changelog(str(tmp_path))

        # v1.0.0 gets its description (no context since empty)
        assert "First stable release" in content
        v1_pos = content.index("## 1.0.0")
        desc1_pos = content.index("First stable release")
        assert desc1_pos > v1_pos

        # v2.0.0 gets both description and context
        assert "Major rewrite" in content
        assert "Migrated from REST to GraphQL" in content
        v2_pos = content.index("## 2.0.0")
        desc2_pos = content.index("Major rewrite")
        ctx2_pos = content.index("Migrated from REST to GraphQL")
        assert v2_pos < desc2_pos < ctx2_pos

        # Context is in a details block
        assert "<details>" in content
        assert "<summary>Context</summary>" in content

    def test_versioned_sections_without_archived_toml_have_no_metadata(
        self, tmp_path, monkeypatch,
    ):
        """Versions without an archived .toml file still generate correctly
        (no description or context)."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="A feat", type="feature"),
                ],
            },
        )
        # No .rlsbl/releases/ directory at all

        content = generate_changelog(str(tmp_path))
        assert "## 1.0.0" in content
        assert "### Features" in content
        assert "<details>" not in content

    def test_per_version_md_files_include_archived_metadata(
        self, tmp_path, monkeypatch,
    ):
        """Per-version .md files written alongside JSONL include description
        and context from archived release tomls."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "3.0.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="New API", type="feature"),
                ],
            },
        )
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "v3.0.0.toml").write_text(
            'bump = "major"\ndescription = "Complete redesign"\n'
            'context = "Old API was unmaintainable"\n'
            'include = ["pypi"]\nexclude = []\n'
        )

        generate_changelog(str(tmp_path))

        md_path = tmp_path / ".rlsbl" / "changes" / "3.0.0.md"
        assert md_path.exists()
        md_content = md_path.read_text()
        assert "Complete redesign" in md_content
        assert "Old API was unmaintainable" in md_content


class TestReadReleaseMetadata:
    """Tests for _read_release_metadata."""

    def test_reads_description_and_context(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "v1.0.0.toml").write_text(
            'bump = "minor"\ndescription = "A release"\ncontext = "Some context"\n'
            'include = ["pypi"]\nexclude = []\n'
        )
        desc, ctx = _read_release_metadata(str(tmp_path), "1.0.0")
        assert desc == "A release"
        assert ctx == "Some context"

    def test_returns_empty_when_no_file(self, tmp_path):
        desc, ctx = _read_release_metadata(str(tmp_path), "1.0.0")
        assert desc == ""
        assert ctx == ""

    def test_returns_empty_context_when_context_empty(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "v2.0.0.toml").write_text(
            'bump = "minor"\ndescription = "Release"\ncontext = ""\n'
            'include = ["pypi"]\nexclude = []\n'
        )
        desc, ctx = _read_release_metadata(str(tmp_path), "2.0.0")
        assert desc == "Release"
        assert ctx == ""

    def test_returns_empty_when_no_description_field(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        # Old-style toml without description/context
        (releases_dir / "v0.1.0.toml").write_text(
            'bump = "patch"\ninclude = ["pypi"]\nexclude = []\n'
        )
        desc, ctx = _read_release_metadata(str(tmp_path), "0.1.0")
        assert desc == ""
        assert ctx == ""

    def test_strips_whitespace(self, tmp_path):
        releases_dir = tmp_path / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "v1.0.0.toml").write_text(
            'bump = "minor"\ndescription = "  padded  "\ncontext = "  also padded  "\n'
            'include = ["pypi"]\nexclude = []\n'
        )
        desc, ctx = _read_release_metadata(str(tmp_path), "1.0.0")
        assert desc == "padded"
        assert ctx == "also padded"


class TestGenerateVersionFileWithMetadata:
    """Tests for generate_version_file with description and context kwargs."""

    def test_passes_description_to_section(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        jsonl = changes / "1.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="A feature", type="feature") + "\n"
        )

        md = generate_version_file(str(changes), "1.0.0", description="Release summary")
        assert "Release summary" in md
        md_path = changes / "1.0.0.md"
        assert "Release summary" in md_path.read_text()

    def test_passes_context_to_section(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        jsonl = changes / "2.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="Break", type="breaking") + "\n"
        )

        md = generate_version_file(str(changes), "2.0.0", context="Needed for perf")
        assert "<details>" in md
        assert "Needed for perf" in md

    def test_no_metadata_by_default(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        jsonl = changes / "1.0.0.jsonl"
        jsonl.write_text(
            _jsonl_line(commits=["a"], user_facing=True, description="Feat", type="feature") + "\n"
        )

        md = generate_version_file(str(changes), "1.0.0")
        assert "<details>" not in md
        lines = md.strip().splitlines()
        assert lines[0] == "## 1.0.0"
        assert lines[1] == ""
        assert lines[2] == "### Features"


class TestBaseVersionHelper:
    """Tests for _base_version."""

    def test_stable_version(self):
        assert _base_version("1.2.3") == "1.2.3"

    def test_alpha_version(self):
        assert _base_version("0.43.0-alpha.0") == "0.43.0"

    def test_beta_version(self):
        assert _base_version("1.0.0-beta.3") == "1.0.0"

    def test_rc_version(self):
        assert _base_version("2.1.0-rc.7") == "2.1.0"


class TestIsPrereleaseHelper:
    """Tests for _is_prerelease."""

    def test_stable(self):
        assert _is_prerelease("1.0.0") is False

    def test_alpha(self):
        assert _is_prerelease("1.0.0-alpha.0") is True

    def test_beta(self):
        assert _is_prerelease("1.0.0-beta.1") is True

    def test_rc(self):
        assert _is_prerelease("1.0.0-rc.0") is True


class TestDeduplicateEntries:
    """Tests for _deduplicate_entries."""

    def test_no_duplicates(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A", type="feature"),
            ChangelogEntry(commits=["b"], user_facing=True, description="B", type="fix"),
        ]
        result = _deduplicate_entries(entries)
        assert len(result) == 2

    def test_removes_exact_duplicate_commits(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A", type="feature"),
            ChangelogEntry(commits=["a"], user_facing=True, description="A copy", type="feature"),
        ]
        result = _deduplicate_entries(entries)
        assert len(result) == 1
        assert result[0].description == "A"  # first wins

    def test_multi_commit_set_dedup(self):
        """Entries with the same set of commits (regardless of order) are deduplicated."""
        entries = [
            ChangelogEntry(commits=["a", "b"], user_facing=True, description="First", type="feature"),
            ChangelogEntry(commits=["b", "a"], user_facing=True, description="Second", type="feature"),
        ]
        result = _deduplicate_entries(entries)
        assert len(result) == 1
        assert result[0].description == "First"

    def test_different_commits_not_deduped(self):
        entries = [
            ChangelogEntry(commits=["a"], user_facing=True, description="A", type="feature"),
            ChangelogEntry(commits=["b"], user_facing=True, description="B", type="feature"),
        ]
        result = _deduplicate_entries(entries)
        assert len(result) == 2


class TestConsolidatedChangelog:
    """Tests for consolidated changelog generation with stable + pre-releases."""

    def _setup_project(self, tmp_path, versions=None, unreleased_lines=None):
        """Helper to set up a project with .rlsbl/changes/."""
        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        if versions:
            for ver, lines in versions.items():
                jsonl = changes / f"{ver}.jsonl"
                jsonl.write_text("\n".join(lines) + "\n")

        if unreleased_lines:
            unreleased = changes / "unreleased.jsonl"
            unreleased.write_text("\n".join(unreleased_lines) + "\n")

        return tmp_path

    def test_consolidated_stable_with_prereleases(self, tmp_path, monkeypatch):
        """When a stable version has pre-release predecessors, entries are consolidated."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Alpha feature", type="feature"),
                ],
                "0.43.0-alpha.1": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Alpha fix", type="fix"),
                ],
                "0.43.0-beta.0": [
                    _jsonl_line(commits=["c"], user_facing=True, description="Beta feature", type="feature"),
                ],
                "0.43.0": [
                    _jsonl_line(commits=["d"], user_facing=True, description="Stable fix", type="fix"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        # Consolidated stable heading exists
        assert "## 0.43.0" in content
        # All entries appear under the stable heading
        assert "Alpha feature" in content
        assert "Alpha fix" in content
        assert "Beta feature" in content
        assert "Stable fix" in content
        # Pre-release cycle note
        assert "Pre-release cycle: 0.43.0-alpha.0, 0.43.0-alpha.1, 0.43.0-beta.0" in content
        # Individual pre-release sub-headings (### level)
        assert "### 0.43.0-alpha.0" in content
        assert "### 0.43.0-alpha.1" in content
        assert "### 0.43.0-beta.0" in content

    def test_consolidated_deduplicates_entries(self, tmp_path, monkeypatch):
        """Entries with the same commits are deduplicated in the consolidated view."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Alpha feature", type="feature"),
                ],
                "1.0.0": [
                    # Same commit hash as in alpha -- should be deduplicated in consolidated view
                    _jsonl_line(commits=["a"], user_facing=True, description="Final feature", type="feature"),
                    _jsonl_line(commits=["b"], user_facing=True, description="New stable fix", type="fix"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        assert "New stable fix" in content

        # In the consolidated ## 1.0.0 section, the entry with commit "a" appears
        # only once (the first occurrence from the stable delta wins because
        # group iteration is newest-first).
        stable_section_start = content.index("## 1.0.0")
        # Find the ### 1.0.0-alpha.0 sub-section start
        alpha_section_start = content.index("### 1.0.0-alpha.0")
        # The consolidated part is between ## 1.0.0 and ### 1.0.0-alpha.0
        consolidated_part = content[stable_section_start:alpha_section_start]
        # Only one feature entry should appear (not both "Alpha feature" and "Final feature")
        assert consolidated_part.count("- ") == 2  # one feature + one fix
        # The alpha sub-section has its own copy of the entry
        alpha_part = content[alpha_section_start:]
        assert "Alpha feature" in alpha_part

    def test_prereleases_only_no_consolidation(self, tmp_path, monkeypatch):
        """When only pre-releases exist (no stable), each gets its own ## heading."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Alpha feature", type="feature"),
                ],
                "0.43.0-alpha.1": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Alpha fix", type="fix"),
                ],
                "0.43.0-beta.0": [
                    _jsonl_line(commits=["c"], user_facing=True, description="Beta feature", type="feature"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        # Each pre-release gets its own ## heading (not consolidated)
        assert "## 0.43.0-beta.0" in content
        assert "## 0.43.0-alpha.1" in content
        assert "## 0.43.0-alpha.0" in content
        # No consolidated stable heading
        assert "## 0.43.0\n" not in content
        # No consolidation note
        assert "Pre-release cycle" not in content

    def test_mixed_stable_and_prerelease_versions(self, tmp_path, monkeypatch):
        """Mixed versions: some consolidated (stable + prereleases), some standalone."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                # 0.42.0 is a standalone stable version
                "0.42.0": [
                    _jsonl_line(commits=["x"], user_facing=True, description="Old feature", type="feature"),
                ],
                # 0.43.0 has pre-releases and a stable
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Alpha feat", type="feature"),
                ],
                "0.43.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Stable feat", type="feature"),
                ],
                # 0.44.0 has only pre-releases (no stable yet)
                "0.44.0-rc.0": [
                    _jsonl_line(commits=["c"], user_facing=True, description="RC feat", type="feature"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        # 0.44.0-rc.0 is standalone pre-release (## heading)
        assert "## 0.44.0-rc.0" in content
        # 0.43.0 is consolidated
        assert "## 0.43.0" in content
        assert "### 0.43.0-alpha.0" in content
        assert "Pre-release cycle: 0.43.0-alpha.0" in content
        # 0.42.0 is standalone stable
        assert "## 0.42.0" in content
        # Correct ordering: 0.44.0-rc.0 > 0.43.0 > 0.42.0
        pos_44 = content.index("## 0.44.0-rc.0")
        pos_43 = content.index("## 0.43.0")
        pos_42 = content.index("## 0.42.0")
        assert pos_44 < pos_43 < pos_42

    def test_consolidated_per_version_md_files_still_created(self, tmp_path, monkeypatch):
        """Per-version .md files are still created for each individual version."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Alpha feat", type="feature"),
                ],
                "0.43.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="Stable feat", type="feature"),
                ],
            },
        )

        generate_changelog(str(tmp_path), write_to_disk=True)

        changes = tmp_path / ".rlsbl" / "changes"
        assert (changes / "0.43.0-alpha.0.md").exists()
        assert (changes / "0.43.0.md").exists()

    def test_consolidated_with_non_user_facing_prereleases(self, tmp_path, monkeypatch):
        """Pre-releases with only non-user-facing entries show 'No user-facing changes.'"""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=False),
                ],
                "0.43.0": [
                    _jsonl_line(commits=["b"], user_facing=True, description="The fix", type="fix"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        # Consolidated section exists
        assert "## 0.43.0" in content
        assert "### 0.43.0-alpha.0" in content
        # The alpha sub-section shows no user-facing changes
        alpha_pos = content.index("### 0.43.0-alpha.0")
        after_alpha = content[alpha_pos:]
        assert "No user-facing changes." in after_alpha

    def test_consolidated_prerelease_cycle_note_ascending_order(self, tmp_path, monkeypatch):
        """The pre-release cycle note lists versions in ascending order."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "1.0.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=False),
                ],
                "1.0.0-alpha.1": [
                    _jsonl_line(commits=["b"], user_facing=False),
                ],
                "1.0.0-beta.0": [
                    _jsonl_line(commits=["c"], user_facing=False),
                ],
                "1.0.0-rc.0": [
                    _jsonl_line(commits=["d"], user_facing=False),
                ],
                "1.0.0": [
                    _jsonl_line(commits=["e"], user_facing=True, description="Final feature", type="feature"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        assert "Pre-release cycle: 1.0.0-alpha.0, 1.0.0-alpha.1, 1.0.0-beta.0, 1.0.0-rc.0" in content

    def test_prerelease_sub_sections_have_type_groups(self, tmp_path, monkeypatch):
        """Pre-release sub-sections use #### headers for type groups."""
        monkeypatch.chdir(tmp_path)
        self._setup_project(
            tmp_path,
            versions={
                "0.43.0-alpha.0": [
                    _jsonl_line(commits=["a"], user_facing=True, description="Breaking!", type="breaking"),
                    _jsonl_line(commits=["b"], user_facing=True, description="New feat", type="feature"),
                ],
                "0.43.0": [
                    _jsonl_line(commits=["c"], user_facing=True, description="A fix", type="fix"),
                ],
            },
        )

        content = generate_changelog(str(tmp_path), write_to_disk=False)

        # Pre-release sub-section uses #### for type groups
        alpha_pos = content.index("### 0.43.0-alpha.0")
        after_alpha = content[alpha_pos:]
        assert "#### Breaking" in after_alpha
        assert "#### Features" in after_alpha
