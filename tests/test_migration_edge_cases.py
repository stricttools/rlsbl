"""Tests for consolidate_changelogs edge cases (migration bugs).

Covers:
- Bug 1: consolidation-point tag at HEAD prevents coverage scope expansion
- Bug 2: batch limit exclusions auto-created for entries exceeding limit
- Bug 3: cross-package commit dedup merges identical-commit entries
"""

import json
import os
import subprocess

import pytest

from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry
from rlsbl.config import read_json_config
from rlsbl.releasable_migration import (
    _dedup_entries,
    _migrate_batch_exclusions,
    _create_consolidation_tag,
    consolidate_changelogs,
)
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    get_releasable_changes_dir,
    get_releasable_dir,
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


def _write_unreleased_jsonl(project_dir, entries):
    """Write changelog entries to a project's unreleased.jsonl."""
    changes_dir = project_dir / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    lines = [serialize_entry(e) + "\n" for e in entries]
    (changes_dir / "unreleased.jsonl").write_text("".join(lines))


def _write_config(project_dir, config):
    """Write .rlsbl/config.json for a project."""
    rlsbl_dir = project_dir / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")


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
# Bug 3: cross-package commit dedup
# ---------------------------------------------------------------------------


class TestDedupEntries:
    """_dedup_entries merges entries with identical commit sets."""

    def test_no_duplicates_unchanged(self):
        entries = [
            ChangelogEntry(commits=["aaa"], user_facing=True,
                           description="Fix A", type="fix", packages=["a"]),
            ChangelogEntry(commits=["bbb"], user_facing=True,
                           description="Fix B", type="fix", packages=["b"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 2
        assert merged_count == 0

    def test_identical_commits_merged(self):
        entries = [
            ChangelogEntry(commits=["aaa", "bbb"], user_facing=True,
                           description="Fix in A", type="fix", packages=["a"]),
            ChangelogEntry(commits=["aaa", "bbb"], user_facing=False,
                           packages=["b"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 1
        assert merged_count == 1
        # User-facing entry should win for description/type
        assert result[0].user_facing is True
        assert result[0].description == "Fix in A"
        assert result[0].type == "fix"
        # Packages combined
        assert result[0].packages == ["a", "b"]

    def test_identical_commits_different_order_merged(self):
        """Commits in different order are still considered identical."""
        entries = [
            ChangelogEntry(commits=["bbb", "aaa"], user_facing=True,
                           description="Fix 1", type="fix", packages=["a"]),
            ChangelogEntry(commits=["aaa", "bbb"], user_facing=True,
                           description="Fix 2", type="fix", packages=["b"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 1
        assert merged_count == 1
        assert set(result[0].packages) == {"a", "b"}

    def test_overlapping_but_different_commits_not_merged(self):
        """Entries with overlapping but non-identical commits stay separate."""
        entries = [
            ChangelogEntry(commits=["aaa", "bbb"], user_facing=True,
                           description="Fix 1", type="fix", packages=["a"]),
            ChangelogEntry(commits=["aaa", "ccc"], user_facing=True,
                           description="Fix 2", type="fix", packages=["b"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 2
        assert merged_count == 0

    def test_three_way_dedup(self):
        """Three entries with identical commits merge into one."""
        entries = [
            ChangelogEntry(commits=["aaa"], user_facing=False, packages=["x"]),
            ChangelogEntry(commits=["aaa"], user_facing=True,
                           description="Feature", type="feature", packages=["y"]),
            ChangelogEntry(commits=["aaa"], user_facing=False, packages=["z"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 1
        assert merged_count == 2
        # User-facing entry provides description
        assert result[0].user_facing is True
        assert result[0].description == "Feature"
        assert result[0].packages == ["x", "y", "z"]

    def test_non_user_facing_only_dedup(self):
        """All non-user-facing entries with same commits merge correctly."""
        entries = [
            ChangelogEntry(commits=["aaa"], user_facing=False, packages=["a"]),
            ChangelogEntry(commits=["aaa"], user_facing=False, packages=["b"]),
        ]
        result, merged_count = _dedup_entries(entries)
        assert len(result) == 1
        assert merged_count == 1
        assert result[0].user_facing is False
        assert result[0].packages == ["a", "b"]


class TestConsolidateDuplicateEntries:
    """consolidate_changelogs deduplicates cross-package entries."""

    def test_duplicate_entries_deduped_in_output(self, tmp_project):
        """Same commit in two packages produces one entry, not two."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        # Both packages reference the same commit
        shared_entry = ChangelogEntry(
            commits=["shared_sha"], user_facing=True,
            description="Shared fix", type="fix",
        )
        _write_unreleased_jsonl(proj_a, [shared_entry])
        _write_unreleased_jsonl(proj_b, [shared_entry])

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)

        # Without dedup: 2 entries. With dedup: 1 entry.
        assert result["entries_merged"] == 1
        assert result["duplicates_merged"] == 1

        entries = parse_jsonl(result["dest_path"])
        assert len(entries) == 1
        assert entries[0].description == "Shared fix"

    def test_mixed_unique_and_duplicate_entries(self, tmp_project):
        """Unique entries preserved alongside deduplicated ones."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        shared = ChangelogEntry(
            commits=["shared"], user_facing=True,
            description="Shared", type="fix",
        )
        unique_a = ChangelogEntry(
            commits=["unique_a"], user_facing=True,
            description="Only A", type="feature",
        )
        unique_b = ChangelogEntry(
            commits=["unique_b"], user_facing=False,
        )

        _write_unreleased_jsonl(proj_a, [shared, unique_a])
        _write_unreleased_jsonl(proj_b, [shared, unique_b])

        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]

        result = consolidate_changelogs(str(tmp_project), "core", members)
        assert result["entries_merged"] == 3  # 1 deduped + 2 unique
        assert result["duplicates_merged"] == 1

        entries = parse_jsonl(result["dest_path"])
        assert len(entries) == 3


# ---------------------------------------------------------------------------
# Bug 2: batch limit exclusions
# ---------------------------------------------------------------------------


class TestMigrateBatchExclusions:
    """_migrate_batch_exclusions creates exclusions for over-limit entries."""

    def test_no_exclusions_when_all_within_limit(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        entries = [
            ChangelogEntry(commits=["c1", "c2", "c3"], user_facing=False),
        ]
        members = [WorkspaceProject({"name": "a", "path": "a"})]
        count = _migrate_batch_exclusions(
            str(tmp_project), "core", members, entries,
        )
        assert count == 0

    def test_exclusion_created_for_over_limit_entry(self, tmp_project):
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        # 7 commits exceeds default max of 5
        entries = [
            ChangelogEntry(
                commits=["c1", "c2", "c3", "c4", "c5", "c6", "c7"],
                user_facing=False,
            ),
        ]
        members = [WorkspaceProject({"name": "a", "path": "a"})]
        count = _migrate_batch_exclusions(
            str(tmp_project), "core", members, entries,
        )
        assert count == 1

        # Verify the exclusion was written
        rel_dir = get_releasable_dir(str(tmp_project), "core")
        config = read_json_config(os.path.join(rel_dir, "config.json"))
        excls = config["batch_limits"]["exclusions"]
        assert len(excls) == 1
        assert excls[0]["reason"] == "auto-created during changelog consolidation"
        assert excls[0]["entries"] == [{"version": "unreleased", "line": 1}]

    def test_uses_smallest_max_from_member_configs(self, tmp_project):
        """Uses the most conservative max_commits_per_entry across members."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        proj_b = _make_pypi_project(tmp_project, "b", "0.1.0")

        # Project a: max 3, project b: max 10
        _write_config(proj_a, {"batch_limits": {"max_commits_per_entry": 3}})
        _write_config(proj_b, {"batch_limits": {"max_commits_per_entry": 10}})

        # 4 commits: exceeds a's limit of 3 but not b's 10
        entries = [
            ChangelogEntry(commits=["c1", "c2", "c3", "c4"], user_facing=False),
        ]
        members = [
            WorkspaceProject({"name": "a", "path": "a"}),
            WorkspaceProject({"name": "b", "path": "b"}),
        ]
        count = _migrate_batch_exclusions(
            str(tmp_project), "core", members, entries,
        )
        assert count == 1

    def test_multiple_entries_get_separate_exclusions(self, tmp_project):
        """Each over-limit entry gets its own exclusion with correct line number."""
        _make_pypi_project(tmp_project, "a", "0.1.0")
        entries = [
            ChangelogEntry(commits=["c1", "c2"], user_facing=False),  # OK
            ChangelogEntry(
                commits=["d1", "d2", "d3", "d4", "d5", "d6"],
                user_facing=True, description="Big change", type="feature",
            ),  # exceeds 5
            ChangelogEntry(commits=["e1"], user_facing=False),  # OK
            ChangelogEntry(
                commits=["f1", "f2", "f3", "f4", "f5", "f6", "f7"],
                user_facing=False,
            ),  # exceeds 5
        ]
        members = [WorkspaceProject({"name": "a", "path": "a"})]
        count = _migrate_batch_exclusions(
            str(tmp_project), "core", members, entries,
        )
        assert count == 2

        rel_dir = get_releasable_dir(str(tmp_project), "core")
        config = read_json_config(os.path.join(rel_dir, "config.json"))
        excls = config["batch_limits"]["exclusions"]
        # Line numbers: entry at index 1 -> line 2, entry at index 3 -> line 4
        lines = [e["entries"][0]["line"] for e in excls]
        assert lines == [2, 4]


class TestConsolidateBatchExclusions:
    """consolidate_changelogs auto-creates batch exclusions during merge."""

    def test_exclusions_created_in_consolidation(self, tmp_project):
        """Over-limit entries from per-package changelogs get exclusions."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")

        # Entry with 7 commits (exceeds default 5)
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(
                commits=["c1", "c2", "c3", "c4", "c5", "c6", "c7"],
                user_facing=True,
                description="Big refactor",
                type="feature",
            ),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "core", members)

        assert result["exclusions_created"] == 1

    def test_no_exclusions_when_within_limit(self, tmp_project):
        """Entries within the limit produce no exclusions."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["c1", "c2"], user_facing=True,
                           description="Small fix", type="fix"),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "core", members)

        assert result["exclusions_created"] == 0


# ---------------------------------------------------------------------------
# Bug 1: consolidation-point tag
# ---------------------------------------------------------------------------


class TestCreateConsolidationTag:
    """_create_consolidation_tag creates a tag at HEAD."""

    def test_creates_tag_at_head(self, tmp_project):
        _init_git(tmp_project)
        _make_commit(tmp_project, "file.txt", "test commit")

        tag = _create_consolidation_tag(
            str(tmp_project), "core", "{name}@v{version}", "0.1.0",
        )
        assert tag == "core@v0.1.0"

        # Verify the tag points to HEAD
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        tag_sha = subprocess.run(
            ["git", "rev-parse", "core@v0.1.0"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head == tag_sha

    def test_returns_none_without_git(self, tmp_project):
        """Returns None when not in a git repo."""
        tag = _create_consolidation_tag(
            str(tmp_project), "core", "{name}@v{version}", "0.1.0",
        )
        assert tag is None

    def test_custom_tag_format(self, tmp_project):
        _init_git(tmp_project)
        tag = _create_consolidation_tag(
            str(tmp_project), "web", "v{version}", "2.0.0",
        )
        assert tag == "v2.0.0"


class TestConsolidateWithConsolidationTag:
    """consolidate_changelogs creates a consolidation-point tag when given format+version."""

    def test_tag_created_with_format_and_version(self, tmp_project):
        _init_git(tmp_project)
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _make_commit(tmp_project, "a/file.txt", "a change")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["aaa"], user_facing=True,
                           description="Fix", type="fix"),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(
            str(tmp_project), "core", members,
            tag_format="{name}@v{version}", version="0.1.0",
        )

        assert result["consolidation_tag"] == "core@v0.1.0"

        # Verify the tag exists in git
        check = subprocess.run(
            ["git", "rev-parse", "core@v0.1.0"],
            cwd=str(tmp_project), capture_output=True, text=True,
        )
        assert check.returncode == 0

    def test_no_tag_without_format_and_version(self, tmp_project):
        """Without tag_format and version, no tag is created."""
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=["aaa"], user_facing=True,
                           description="Fix", type="fix"),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(str(tmp_project), "core", members)

        assert result["consolidation_tag"] is None

    def test_tag_points_to_head(self, tmp_project):
        """The consolidation-point tag should point to the current HEAD."""
        _init_git(tmp_project)
        proj_a = _make_pypi_project(tmp_project, "a", "0.1.0")
        sha = _make_commit(tmp_project, "a/code.py", "code change")

        _write_unreleased_jsonl(proj_a, [
            ChangelogEntry(commits=[sha], user_facing=False),
        ])

        members = [WorkspaceProject({"name": "a", "path": "a"})]
        result = consolidate_changelogs(
            str(tmp_project), "core", members,
            tag_format="{name}@v{version}", version="0.1.0",
        )

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        tag_sha = subprocess.run(
            ["git", "rev-parse", result["consolidation_tag"]],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head == tag_sha
