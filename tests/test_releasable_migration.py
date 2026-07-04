"""Tests for releasable migration tooling (Phase 10).

Covers:
- detect_migration_state: already explicit, implicit with mixed versions,
  suggested groupings, dev_node handling, changelog detection
- consolidate_changelogs: merge entries, packages field derivation
- consolidate_versions: matching versions, conflicting versions, empty
- create_migration_tag: tag creation, format, no tags, most recent selection
- _extract_version_from_tag: various tag formats
- _read_project_version: target detection and version reading
"""

import json
import os
import subprocess

import pytest

from rlsbl.changelog.schema import ChangelogEntry, serialize_entry
from rlsbl.releasable_migration import (
    _derive_packages_for_entry,
    _extract_version_from_tag,
    _read_project_version,
    consolidate_changelogs,
    consolidate_versions,
    create_migration_tag,
    detect_migration_state,
)
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    get_releasable_changes_dir,
    read_releasable_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _make_pypi_project(base, name, version="0.1.0"):
    """Create a minimal pypi project directory with pyproject.toml."""
    proj_dir = base / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return proj_dir


def _make_npm_project(base, name, version="0.1.0"):
    """Create a minimal npm project directory with package.json."""
    proj_dir = base / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "package.json").write_text(
        json.dumps({"name": name, "version": version})
    )
    return proj_dir


def _write_unreleased_jsonl(project_dir, entries):
    """Write changelog entries to a project's unreleased.jsonl."""
    changes_dir = project_dir / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    lines = [serialize_entry(e) + "\n" for e in entries]
    (changes_dir / "unreleased.jsonl").write_text("".join(lines))


def _write_versioned_jsonl(project_dir, version, entries):
    """Write changelog entries to a project's versioned .jsonl file."""
    changes_dir = project_dir / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    lines = [serialize_entry(e) + "\n" for e in entries]
    (changes_dir / f"{version}.jsonl").write_text("".join(lines))


def _init_git(path):
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"],
                   cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(path), check=True)
    readme = path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"],
                   cwd=str(path), check=True)


def _make_commit(path, filename, message="change"):
    """Create a file and commit it, returning the commit SHA."""
    filepath = path / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(f"content of {filename}\n")
    subprocess.run(["git", "add", str(filename)], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(path), check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# detect_migration_state: already explicit
# ---------------------------------------------------------------------------


class TestDetectMigrationStateExplicit:
    """detect_migration_state when workspace is already in explicit mode."""

    def test_explicit_mode_detected(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["explicit_mode"] is True

    def test_explicit_mode_project_reports_releasable_field(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        result = detect_migration_state(str(tmp_project))
        proj_info = result["projects"][0]
        assert proj_info["releasable"] == "core"


# ---------------------------------------------------------------------------
# detect_migration_state: implicit mode
# ---------------------------------------------------------------------------


class TestDetectMigrationStateImplicit:
    """detect_migration_state in implicit mode (no [[releasables]])."""

    def test_implicit_mode_detected(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["explicit_mode"] is False

    def test_reports_project_version(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.2.3")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["projects"][0]["version"] == "1.2.3"

    def test_reports_no_version_for_missing_manifest(self, tmp_project):
        proj_dir = tmp_project / "a"
        proj_dir.mkdir()
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["projects"][0]["version"] is None

    def test_reports_npm_version(self, tmp_project):
        _make_npm_project(tmp_project, "webui", "2.0.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "webui"
name = "webui"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["projects"][0]["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# detect_migration_state: changelog detection
# ---------------------------------------------------------------------------


class TestDetectMigrationStateChangelog:
    """detect_migration_state reports changelog presence and entry count."""

    def test_has_changelog_true(self, tmp_project):
        proj_dir = _make_pypi_project(tmp_project, "a", "0.1.0")
        entries = [
            ChangelogEntry(commits=["abc1234"], user_facing=True,
                           description="Fix bug", type="fix"),
        ]
        _write_unreleased_jsonl(proj_dir, entries)
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        info = result["projects"][0]
        assert info["has_changelog"] is True
        assert info["unreleased_entry_count"] == 1

    def test_has_changelog_false(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        info = result["projects"][0]
        assert info["has_changelog"] is False
        assert info["unreleased_entry_count"] == 0

    def test_multiple_unreleased_entries(self, tmp_project):
        proj_dir = _make_pypi_project(tmp_project, "a", "0.1.0")
        entries = [
            ChangelogEntry(commits=["abc1234"], user_facing=True,
                           description="Fix bug", type="fix"),
            ChangelogEntry(commits=["def5678"], user_facing=False),
            ChangelogEntry(commits=["ghi9012"], user_facing=True,
                           description="New feature", type="feature"),
        ]
        _write_unreleased_jsonl(proj_dir, entries)
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["projects"][0]["unreleased_entry_count"] == 3

    def test_versioned_file_count(self, tmp_project):
        proj_dir = _make_pypi_project(tmp_project, "a", "0.2.0")
        _write_unreleased_jsonl(proj_dir, [])
        _write_versioned_jsonl(
            proj_dir, "0.1.0",
            [ChangelogEntry(commits=["abc"], user_facing=False)],
        )
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["projects"][0]["versioned_file_count"] == 1


# ---------------------------------------------------------------------------
# detect_migration_state: suggested groupings
# ---------------------------------------------------------------------------


class TestDetectMigrationStateSuggestedGroupings:
    """detect_migration_state suggests groupings for projects sharing a version."""

    def test_same_version_grouped(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "1.0.0")
        _make_pypi_project(tmp_project, "c", "2.0.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "b"
name = "b"

[[projects]]
path = "c"
name = "c"
""")
        result = detect_migration_state(str(tmp_project))
        assert "1.0.0" in result["suggested_groupings"]
        assert set(result["suggested_groupings"]["1.0.0"]) == {"a", "b"}
        # c is at 2.0.0, alone -- not suggested
        assert "2.0.0" not in result["suggested_groupings"]

    def test_all_same_version(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "b", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "b"
name = "b"
""")
        result = detect_migration_state(str(tmp_project))
        assert "0.1.0" in result["suggested_groupings"]
        assert set(result["suggested_groupings"]["0.1.0"]) == {"a", "b"}

    def test_all_different_no_suggestions(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "2.0.0")
        _make_pypi_project(tmp_project, "c", "3.0.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "b"
name = "b"

[[projects]]
path = "c"
name = "c"
""")
        result = detect_migration_state(str(tmp_project))
        assert result["suggested_groupings"] == {}


# ---------------------------------------------------------------------------
# detect_migration_state: dev_node handling
# ---------------------------------------------------------------------------


class TestDetectMigrationStateDevNode:
    """detect_migration_state properly reports and excludes dev_node projects."""

    def test_dev_node_reported(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "tests", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "tests"
name = "tests"
dev_node = true
""")
        result = detect_migration_state(str(tmp_project))
        tests_info = [p for p in result["projects"] if p["name"] == "tests"][0]
        assert tests_info["dev_node"] is True

    def test_dev_node_excluded_from_groupings(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "tests", "0.1.0")
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "a"

[[projects]]
path = "tests"
name = "tests"
dev_node = true
""")
        result = detect_migration_state(str(tmp_project))
        # "a" alone at 0.1.0 (tests excluded) -- no grouping suggested
        assert result["suggested_groupings"] == {}


# ---------------------------------------------------------------------------
# consolidate_changelogs: merge entries
# ---------------------------------------------------------------------------


class TestConsolidateChangelogsMerge:
    """consolidate_changelogs merges entries from member projects."""

    def test_merges_entries_from_two_projects(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["aaa1111"], user_facing=True,
                           description="Fix in a", type="fix"),
        ])
        _write_unreleased_jsonl(proj_b, [
            ChangelogEntry(commits=["bbb2222"], user_facing=True,
                           description="Feature in b", type="feature"),
        ])

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)

        assert result["entries_merged"] == 2
        assert set(result["source_projects"]) == {"a", "b"}
        assert os.path.isfile(result["dest_path"])

    def test_empty_projects_skipped(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["aaa1111"], user_facing=True,
                           description="Fix in a", type="fix"),
        ])
        _write_unreleased_jsonl(proj_b, [])  # empty

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)
        assert result["entries_merged"] == 1
        assert result["source_projects"] == ["a"]

    def test_no_entries_anywhere(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_pypi_project(tmp_project, "b", "0.1.0")

        # No changelog dirs at all
        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)
        assert result["entries_merged"] == 0
        assert result["source_projects"] == []

    def test_dest_file_written_to_releasable_dir(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["aaa1111"], user_facing=False),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "myrel", members)

        expected_dir = get_releasable_changes_dir(str(tmp_project), "myrel")
        assert result["dest_path"] == os.path.join(expected_dir, "unreleased.jsonl")

    def test_merged_entries_preserve_fields(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(
                commits=["aaa1111", "aaa2222"],
                user_facing=True,
                description="**Breaking.** API changed",
                type="breaking",
            ),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "core", members)

        # Read back the merged file
        from rlsbl.changelog.schema import parse_jsonl
        entries = parse_jsonl(result["dest_path"])
        assert len(entries) == 1
        assert entries[0].commits == ["aaa1111", "aaa2222"]
        assert entries[0].user_facing is True
        assert entries[0].description == "**Breaking.** API changed"
        assert entries[0].type == "breaking"


# ---------------------------------------------------------------------------
# consolidate_changelogs: packages field derivation
# ---------------------------------------------------------------------------


class TestConsolidateChangelogsPackagesField:
    """consolidate_changelogs derives the packages field from commit paths."""

    def test_packages_derived_in_git_repo(self, tmp_project):
        """In a real git repo, packages field is derived from commit file paths."""
        _init_git(tmp_project)

        # Create project dirs
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        # Make a commit touching project a
        sha = _make_commit(tmp_project, "a/src/main.py", "change in a")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=[sha], user_facing=True,
                           description="Fix", type="fix"),
        ])

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)

        from rlsbl.changelog.schema import parse_jsonl
        entries = parse_jsonl(result["dest_path"])
        assert len(entries) == 1
        # The commit only touched 'a/', so packages should be ["a"]
        assert entries[0].packages == ["a"]

    def test_packages_none_when_no_git(self, tmp_project):
        """Without git, packages derivation returns None (no git data)."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["fake_sha"], user_facing=True,
                           description="Fix", type="fix"),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "core", members)

        from rlsbl.changelog.schema import parse_jsonl
        entries = parse_jsonl(result["dest_path"])
        # Cannot derive packages without git -- should be None
        assert entries[0].packages is None


# ---------------------------------------------------------------------------
# consolidate_changelogs: versioned file merge
# ---------------------------------------------------------------------------


class TestConsolidateChangelogsVersionedFiles:
    """consolidate_changelogs merges versioned JSONL files across members."""

    def test_consolidate_changelogs_merges_versioned_files(self, tmp_project):
        """Versioned JSONL files from members are merged into the releasable,
        with entries tagged by packages field."""
        proj_auth = _make_pypi_project(tmp_project, "auth", "0.2.0")
        proj_api = _make_pypi_project(tmp_project, "api", "0.2.0")

        # Both have 0.1.0.jsonl with different entries
        _write_versioned_jsonl(proj_auth, "0.1.0", [
            ChangelogEntry(commits=["aaa1111"], user_facing=True,
                           description="Auth login feature", type="feature"),
        ])
        _write_versioned_jsonl(proj_api, "0.1.0", [
            ChangelogEntry(commits=["bbb2222"], user_facing=True,
                           description="API endpoint added", type="feature"),
        ])

        # Only api has 0.2.0.jsonl
        _write_versioned_jsonl(proj_api, "0.2.0", [
            ChangelogEntry(commits=["ccc3333"], user_facing=True,
                           description="API rate limiting", type="feature"),
        ])

        # Both have empty unreleased
        _write_unreleased_jsonl(proj_auth, [])
        _write_unreleased_jsonl(proj_api, [])

        members = [
            WorkspaceProject({"name": "api", "path": "api"}),
            WorkspaceProject({"name": "auth", "path": "auth"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)

        # Check the releasable's changes dir for versioned files
        from rlsbl.changelog.schema import parse_jsonl
        dest_changes = get_releasable_changes_dir(str(tmp_project), "core")

        # 0.1.0.jsonl should exist with entries from BOTH members
        merged_010_path = os.path.join(dest_changes, "0.1.0.jsonl")
        assert os.path.isfile(merged_010_path), \
            f"Expected merged 0.1.0.jsonl at {merged_010_path}"
        entries_010 = parse_jsonl(merged_010_path)
        assert len(entries_010) == 2
        descriptions = {e.description for e in entries_010}
        assert "Auth login feature" in descriptions
        assert "API endpoint added" in descriptions
        # Entries should be tagged with packages
        for entry in entries_010:
            assert entry.packages is not None

        # 0.2.0.jsonl should exist with api's entry
        merged_020_path = os.path.join(dest_changes, "0.2.0.jsonl")
        assert os.path.isfile(merged_020_path), \
            f"Expected merged 0.2.0.jsonl at {merged_020_path}"
        entries_020 = parse_jsonl(merged_020_path)
        assert len(entries_020) == 1
        assert entries_020[0].description == "API rate limiting"
        assert entries_020[0].packages == ["api"]


# ---------------------------------------------------------------------------
# _derive_packages_for_entry (unit tests)
# ---------------------------------------------------------------------------


class TestDerivePackagesForEntry:
    """_derive_packages_for_entry correctly maps commits to projects."""

    def test_returns_sorted_unique(self, tmp_project):
        """Results are sorted and deduplicated."""
        _init_git(tmp_project)
        (tmp_project / "b").mkdir(exist_ok=True)
        (tmp_project / "a").mkdir(exist_ok=True)

        sha = _make_commit(tmp_project, "b/file.txt", "touch b")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        entry = ChangelogEntry(commits=[sha], user_facing=False)
        result = _derive_packages_for_entry(entry, members, str(tmp_project))
        assert result == ["b"]

    def test_commit_touching_multiple_projects(self, tmp_project):
        """A commit touching files in multiple projects lists all of them."""
        _init_git(tmp_project)
        (tmp_project / "a").mkdir(exist_ok=True)
        (tmp_project / "b").mkdir(exist_ok=True)

        # Create files in both projects in one commit
        (tmp_project / "a" / "f.txt").write_text("a\n")
        (tmp_project / "b" / "f.txt").write_text("b\n")
        subprocess.run(["git", "add", "a/f.txt", "b/f.txt"],
                       cwd=str(tmp_project), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "both"],
                       cwd=str(tmp_project), check=True)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        )
        sha = result.stdout.strip()

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        entry = ChangelogEntry(commits=[sha], user_facing=False)
        packages = _derive_packages_for_entry(entry, members, str(tmp_project))
        assert packages == ["a", "b"]

    def test_empty_when_no_files_match(self, tmp_project):
        """Returns empty list when commit files match no projects."""
        _init_git(tmp_project)
        sha = _make_commit(tmp_project, "root-file.txt", "root change")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
        ]

        entry = ChangelogEntry(commits=[sha], user_facing=False)
        packages = _derive_packages_for_entry(entry, members, str(tmp_project))
        assert packages == []


# ---------------------------------------------------------------------------
# consolidate_versions: matching versions
# ---------------------------------------------------------------------------


class TestConsolidateVersionsMatching:
    """consolidate_versions when all members share the same version."""

    def test_writes_common_version(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "1.0.0")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        assert result["status"] == "ok"
        assert result["version"] == "1.0.0"

        # Version file should exist
        written = read_releasable_version(str(tmp_project), "core")
        assert written == "1.0.0"

    def test_single_member(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "2.5.0")

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_versions(str(tmp_project), "solo", members)
        assert result["status"] == "ok"
        assert result["version"] == "2.5.0"

    def test_versions_dict_populated(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "1.0.0")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        assert result["versions"] == {"a": "1.0.0", "b": "1.0.0"}


# ---------------------------------------------------------------------------
# consolidate_versions: conflicting versions
# ---------------------------------------------------------------------------


class TestConsolidateVersionsConflict:
    """consolidate_versions when members have different versions."""

    def test_conflict_detected(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "2.0.0")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        assert result["status"] == "conflict"
        assert result["version"] is None

    def test_conflict_reports_all_versions(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "2.0.0")
        _make_pypi_project(tmp_project, "c", "1.0.0")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
            WorkspaceProject({"name": "c", "path": "c"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        assert result["status"] == "conflict"
        assert result["versions"] == {"a": "1.0.0", "b": "2.0.0", "c": "1.0.0"}

    def test_conflict_does_not_write_version_file(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_pypi_project(tmp_project, "b", "2.0.0")

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        consolidate_versions(str(tmp_project), "core", members)

        # Version file should NOT have been written
        from rlsbl.workspace import get_releasable_version_path
        vpath = get_releasable_version_path(str(tmp_project), "core")
        assert not os.path.exists(vpath)


# ---------------------------------------------------------------------------
# consolidate_versions: no versions found
# ---------------------------------------------------------------------------


class TestConsolidateVersionsEmpty:
    """consolidate_versions when no member has a detectable version."""

    def test_empty_status(self, tmp_project):
        # Project dirs exist but no manifest files
        (tmp_project / "a").mkdir()
        (tmp_project / "b").mkdir()

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        assert result["status"] == "empty"
        assert result["version"] is None
        assert result["versions"] == {}

    def test_mix_of_detectable_and_not(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "1.0.0")
        (tmp_project / "b").mkdir()  # no manifest

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_versions(str(tmp_project), "core", members)
        # Only one version found -> ok (one unique version)
        assert result["status"] == "ok"
        assert result["version"] == "1.0.0"
        assert result["versions"] == {"a": "1.0.0"}


# ---------------------------------------------------------------------------
# create_migration_tag: tag creation
# ---------------------------------------------------------------------------


class TestCreateMigrationTag:
    """create_migration_tag creates releasable-format tags."""

    def test_creates_tag_from_per_package_tag(self, tmp_project):
        _init_git(tmp_project)

        # Create a project and tag it
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(
            ["git", "tag", "a@v0.1.0"],
            cwd=str(tmp_project), check=True,
        )

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert result["status"] == "created"
        assert result["tag"] == "core@v0.1.0"
        assert result["source_tag"] == "a@v0.1.0"
        assert result["commit"] is not None

        # Verify the tag exists in git
        check = subprocess.run(
            ["git", "rev-parse", "core@v0.1.0"],
            cwd=str(tmp_project), capture_output=True, text=True,
        )
        assert check.returncode == 0

    def test_picks_most_recent_across_members(self, tmp_project):
        _init_git(tmp_project)

        # Create first project and tag
        _make_pypi_project(tmp_project, "a", "0.1.0")
        sha1 = _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(
            ["git", "tag", "a@v0.1.0"],
            cwd=str(tmp_project), check=True,
        )

        # Create second project and tag (later commit = more recent)
        _make_pypi_project(tmp_project, "b", "0.2.0")
        sha2 = _make_commit(tmp_project, "b/src.py", "initial b")
        subprocess.run(
            ["git", "tag", "b@v0.2.0"],
            cwd=str(tmp_project), check=True,
        )

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert result["status"] == "created"
        # Should use the more recent tag (b@v0.2.0)
        assert result["source_tag"] == "b@v0.2.0"
        assert result["tag"] == "core@v0.2.0"

    def test_no_tags_returns_no_tags_status(self, tmp_project):
        _init_git(tmp_project)
        _make_pypi_project(tmp_project, "a", "0.1.0")

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert result["status"] == "no_tags"
        assert result["tag"] is None

    def test_custom_tag_format(self, tmp_project):
        _init_git(tmp_project)

        _make_pypi_project(tmp_project, "a", "1.0.0")
        _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(
            ["git", "tag", "a@v1.0.0"],
            cwd=str(tmp_project), check=True,
        )

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = create_migration_tag(
            str(tmp_project), "www", "v{version}", members,
        )

        assert result["status"] == "created"
        assert result["tag"] == "v1.0.0"

    def test_member_tags_populated(self, tmp_project):
        _init_git(tmp_project)

        _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_commit(tmp_project, "a/src.py", "initial a")
        subprocess.run(
            ["git", "tag", "a@v0.1.0"],
            cwd=str(tmp_project), check=True,
        )

        _make_pypi_project(tmp_project, "b", "0.2.0")
        _make_commit(tmp_project, "b/src.py", "initial b")
        subprocess.run(
            ["git", "tag", "b@v0.2.0"],
            cwd=str(tmp_project), check=True,
        )

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = create_migration_tag(
            str(tmp_project), "core", "{name}@v{version}", members,
        )

        assert "a" in result["member_tags"]
        assert "b" in result["member_tags"]
        assert result["member_tags"]["a"] == "a@v0.1.0"
        assert result["member_tags"]["b"] == "b@v0.2.0"


# ---------------------------------------------------------------------------
# _extract_version_from_tag (unit tests)
# ---------------------------------------------------------------------------


class TestExtractVersionFromTag:
    """_extract_version_from_tag handles various tag formats."""

    def test_simple_v_prefix(self):
        assert _extract_version_from_tag("v1.2.3") == "1.2.3"

    def test_name_at_v_prefix(self):
        assert _extract_version_from_tag("mylib@v0.5.0") == "0.5.0"

    def test_path_v_prefix(self):
        assert _extract_version_from_tag("packages/core/v1.0.0") == "1.0.0"

    def test_prerelease(self):
        assert _extract_version_from_tag("v1.0.0-rc.1") == "1.0.0-rc.1"

    def test_no_version(self):
        assert _extract_version_from_tag("release-candidate") is None

    def test_bare_version_without_v(self):
        """Without 'v' prefix, extraction fails (intentional)."""
        assert _extract_version_from_tag("1.2.3") is None

    def test_zero_version(self):
        assert _extract_version_from_tag("v0.0.0") == "0.0.0"


# ---------------------------------------------------------------------------
# _read_project_version (unit tests)
# ---------------------------------------------------------------------------


class TestReadProjectVersion:
    """_read_project_version reads versions from various target types."""

    def test_pypi_version(self, tmp_project):
        _make_pypi_project(tmp_project, "a", "3.2.1")
        result = _read_project_version(str(tmp_project / "a"))
        assert result == "3.2.1"

    def test_npm_version(self, tmp_project):
        _make_npm_project(tmp_project, "webui", "4.0.0")
        result = _read_project_version(str(tmp_project / "webui"))
        assert result == "4.0.0"

    def test_no_manifest_returns_none(self, tmp_project):
        empty_dir = tmp_project / "empty"
        empty_dir.mkdir()
        result = _read_project_version(str(empty_dir))
        assert result is None
