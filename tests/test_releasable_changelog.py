"""Tests for Phase 4: changelog per releasable.

Covers:
- get_releasable_changes_dir path resolution
- _get_changelog_context returns releasable changes dir in explicit mode
- filter_commits_for_releasable with multiple projects
- check_coverage scoped to releasable
- changelog add writes to releasable changes dir
- packages field on ChangelogEntry (optional, backward compat)
- CHANGELOG.md generation per releasable
- Cache per releasable
"""

import json
import os
import subprocess
import time

import pytest

from rlsbl.changelog.files import (
    append_entry,
    finalize_version,
    get_changes_dir,
    read_unreleased,
)
from rlsbl.changelog.generate import generate_changelog
from rlsbl.changelog.schema import (
    ChangelogEntry,
    parse_entry,
    serialize_entry,
    validate_schema,
)
from rlsbl.changelog.validate import (
    _filter_commits_for_scope,
    _read_all_versioned_entries,
    check_coverage,
    check_in_range,
    check_no_orphans,
)
from rlsbl.git_util import filter_commits_for_releasable
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    get_releasable_changes_dir,
    get_releasable_dir,
    is_explicit_mode,
    load_releasables,
    members_of,
    resolve_releasable_for_project,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)

from conftest import make_commit, make_workspace, run_git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace_explicit(tmp_path, releasables, projects):
    """Write a workspace.toml with explicit releasable definitions."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    lines = []
    for rel in releasables:
        lines.append("[[releasables]]")
        lines.append(f'name = "{rel["name"]}"')
        if "tag_format" in rel:
            lines.append(f'tag_format = "{rel["tag_format"]}"')
        lines.append("")
    for proj in projects:
        lines.append("[[projects]]")
        lines.append(f'path = "{proj["path"]}"')
        lines.append(f'name = "{proj["name"]}"')
        if "releasable" in proj:
            val = proj["releasable"]
            if isinstance(val, bool) and val is False:
                lines.append("releasable = false")
            elif isinstance(val, str):
                lines.append(f'releasable = "{val}"')
        if "watch" in proj:
            watch_items = ", ".join(f'"{w}"' for w in proj["watch"])
            lines.append(f"watch = [{watch_items}]")
        if proj.get("dev_node"):
            lines.append("dev_node = true")
        lines.append("")
    (ws_dir / WORKSPACE_FILE).write_text("\n".join(lines))


def _setup_releasable_changes(tmp_path, releasable_name, entries=None):
    """Create a releasable changes directory with an unreleased.jsonl file."""
    changes_dir = get_releasable_changes_dir(str(tmp_path), releasable_name)
    os.makedirs(changes_dir, exist_ok=True)
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    if entries:
        lines = [serialize_entry(e) + "\n" for e in entries]
        with open(unreleased, "w", encoding="utf-8") as f:
            f.writelines(lines)
    else:
        with open(unreleased, "w", encoding="utf-8") as f:
            pass
    return changes_dir


# ---------------------------------------------------------------------------
# get_releasable_changes_dir path resolution
# ---------------------------------------------------------------------------


class TestGetReleasableChangesDir:
    """get_releasable_changes_dir returns the correct path."""

    def test_basic_path(self, tmp_path):
        result = get_releasable_changes_dir(str(tmp_path), "core")
        expected = os.path.join(str(tmp_path), ".rlsbl-monorepo", "releasables", "core", "changes")
        assert result == expected

    def test_different_names(self, tmp_path):
        for name in ["core", "www", "my-rel", "rel_underscore"]:
            result = get_releasable_changes_dir(str(tmp_path), name)
            assert name in result
            assert result.endswith("changes")

    def test_path_is_under_releasable_dir(self, tmp_path):
        changes = get_releasable_changes_dir(str(tmp_path), "core")
        rel_dir = get_releasable_dir(str(tmp_path), "core")
        assert changes.startswith(rel_dir)

    def test_creates_correct_structure(self, tmp_path):
        changes_dir = get_releasable_changes_dir(str(tmp_path), "myrel")
        os.makedirs(changes_dir, exist_ok=True)
        assert os.path.isdir(changes_dir)
        # Verify intermediate dirs
        assert os.path.isdir(os.path.join(str(tmp_path), ".rlsbl-monorepo", "releasables", "myrel"))


# ---------------------------------------------------------------------------
# resolve_releasable_for_project
# ---------------------------------------------------------------------------


class TestResolveReleasableForProject:
    """resolve_releasable_for_project finds the right releasable."""

    def test_explicit_membership(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "releasable": "core"})
        rels = [Releasable(name="core"), Releasable(name="www")]
        result = resolve_releasable_for_project(proj, rels)
        assert result is not None
        assert result.name == "core"

    def test_no_releasable_field_returns_none(self):
        """Project without releasable field does not match any releasable."""
        proj = WorkspaceProject({"name": "alpha", "path": "alpha"})
        rels = [Releasable(name="alpha"), Releasable(name="beta")]
        result = resolve_releasable_for_project(proj, rels)
        assert result is None

    def test_false_releasable_returns_none(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "releasable": False})
        rels = [Releasable(name="core")]
        result = resolve_releasable_for_project(proj, rels)
        assert result is None

    def test_no_match(self):
        proj = WorkspaceProject({"name": "a", "path": "a", "releasable": "nonexistent"})
        rels = [Releasable(name="core")]
        result = resolve_releasable_for_project(proj, rels)
        assert result is None

    def test_dict_project(self):
        proj = {"name": "a", "path": "a", "releasable": "core"}
        rels = [Releasable(name="core")]
        result = resolve_releasable_for_project(proj, rels)
        assert result is not None
        assert result.name == "core"


# ---------------------------------------------------------------------------
# _get_changelog_context in explicit mode
# ---------------------------------------------------------------------------


class TestGetChangelogContextExplicitMode:
    """_get_changelog_context returns releasable changes dir in explicit mode."""

    def test_explicit_mode_returns_releasable_changes_dir(self, tmp_path, monkeypatch):
        """In explicit mode, changes_dir points to the releasable's dir."""
        monkeypatch.chdir(tmp_path)

        _write_workspace_explicit(tmp_path,
            releasables=[{"name": "core"}],
            projects=[
                {"path": "a", "name": "a", "releasable": "core"},
                {"path": "b", "name": "b", "releasable": "core"},
            ],
        )

        # Create project dirs
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()

        # Create releasable changes dir
        changes_dir = _setup_releasable_changes(tmp_path, "core")

        from pathlib import Path
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects=projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path) / "a",
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            releasables=releasables,
        )

        from rlsbl.checks._common import _get_changelog_context
        result = _get_changelog_context(ctx)
        assert result is not None
        resolved_dir, tag_glob, project, entries = result
        assert resolved_dir == changes_dir
        # tag_glob should be derived from releasable's tag_format
        assert tag_glob == "core@v*"
        # project should be a list of member projects
        assert isinstance(project, list)
        assert len(project) == 2
        member_names = {p.name for p in project}
        assert member_names == {"a", "b"}

    def test_no_releasables_returns_per_project_changes_dir(self, tmp_path, monkeypatch):
        """Without [[releasables]], changes_dir is per-project."""
        monkeypatch.chdir(tmp_path)

        # Create workspace without releasables
        make_workspace(tmp_path, [
            {"path": "a", "name": "alpha"},
        ])

        # Create project and its changes dir
        proj_dir = tmp_path / "a"
        proj_dir.mkdir()
        changes_dir = proj_dir / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        # Create a pyproject.toml for target detection
        (proj_dir / "pyproject.toml").write_text('[project]\nname = "alpha"\nversion = "0.1.0"\n')

        from pathlib import Path
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(tmp_path))

        ctx = WorkspaceCheckContext(
            project_root=Path(proj_dir),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            releasables=[],
        )

        from rlsbl.checks._common import _get_changelog_context
        result = _get_changelog_context(ctx)
        assert result is not None
        resolved_dir, tag_glob, project, entries = result
        assert resolved_dir == str(changes_dir)
        # Without releasables, project is a single WorkspaceProject, not a list
        assert not isinstance(project, list)

    def test_non_releasable_project_returns_none(self, tmp_path, monkeypatch):
        """In explicit mode, a project with releasable=false returns None."""
        monkeypatch.chdir(tmp_path)

        _write_workspace_explicit(tmp_path,
            releasables=[{"name": "core"}],
            projects=[
                {"path": "a", "name": "a", "releasable": "core"},
                {"path": "b", "name": "b", "releasable": False},
            ],
        )

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        _setup_releasable_changes(tmp_path, "core")

        from pathlib import Path
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects=projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path) / "b",
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            releasables=releasables,
        )

        from rlsbl.checks._common import _get_changelog_context
        result = _get_changelog_context(ctx)
        assert result is None

    def test_custom_tag_format(self, tmp_path, monkeypatch):
        """tag_glob is derived from the releasable's tag_format."""
        monkeypatch.chdir(tmp_path)

        _write_workspace_explicit(tmp_path,
            releasables=[{"name": "www", "tag_format": "v{version}"}],
            projects=[
                {"path": "a", "name": "a", "releasable": "www"},
            ],
        )
        (tmp_path / "a").mkdir()
        _setup_releasable_changes(tmp_path, "www")

        from pathlib import Path
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects=projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path) / "a",
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            releasables=releasables,
        )

        from rlsbl.checks._common import _get_changelog_context
        result = _get_changelog_context(ctx)
        assert result is not None
        _, tag_glob, _, _ = result
        assert tag_glob == "v*"


# ---------------------------------------------------------------------------
# filter_commits_for_releasable
# ---------------------------------------------------------------------------


class TestFilterCommitsForReleasable:
    """filter_commits_for_releasable with multiple projects."""

    def test_filters_across_multiple_projects(self, mock_git_repo):
        """Commits touching any member project's files are included."""
        root = mock_git_repo

        # Create two project dirs
        (root / "pkg-a").mkdir()
        (root / "pkg-b").mkdir()

        # Commit in pkg-a
        sha_a = make_commit(root, "pkg-a/code.py", "change in a")
        # Commit in pkg-b
        sha_b = make_commit(root, "pkg-b/code.py", "change in b")
        # Commit outside both
        sha_other = make_commit(root, "readme.txt", "top-level change")

        projects = [
            WorkspaceProject({"name": "a", "path": "pkg-a"}),
            WorkspaceProject({"name": "b", "path": "pkg-b"}),
        ]

        result = filter_commits_for_releasable({sha_a, sha_b, sha_other}, projects)
        assert sha_a in result
        assert sha_b in result
        assert sha_other not in result

    def test_single_project_behaves_like_filter_for_project(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()

        sha = make_commit(root, "pkg/code.py", "change")
        sha_other = make_commit(root, "other.txt", "other")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]

        result = filter_commits_for_releasable({sha, sha_other}, projects)
        assert sha in result
        assert sha_other not in result

    def test_empty_projects_returns_empty(self, mock_git_repo):
        root = mock_git_repo
        sha = make_commit(root, "file.txt", "change")
        result = filter_commits_for_releasable({sha}, [])
        assert len(result) == 0

    def test_watch_globs_included(self, mock_git_repo):
        """Watch globs on member projects are also matched."""
        root = mock_git_repo
        (root / "pkg").mkdir()
        (root / "shared").mkdir()

        sha = make_commit(root, "shared/config.json", "config change")

        projects = [
            WorkspaceProject({"name": "p", "path": "pkg", "watch": ["shared/*"]}),
        ]
        result = filter_commits_for_releasable({sha}, projects)
        assert sha in result


# ---------------------------------------------------------------------------
# _filter_commits_for_scope dispatcher
# ---------------------------------------------------------------------------


class TestFilterCommitsForScope:
    """_filter_commits_for_scope dispatches correctly."""

    def test_none_project_returns_unchanged(self):
        commits = {"aaa", "bbb", "ccc"}
        result = _filter_commits_for_scope(commits, None)
        assert result == commits

    def test_list_project_calls_releasable_filter(self, mock_git_repo, monkeypatch):
        """When project is a list, delegates to filter_commits_for_releasable."""
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]
        result = _filter_commits_for_scope({sha}, projects)
        assert sha in result

    def test_single_project_calls_project_filter(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        project = WorkspaceProject({"name": "p", "path": "pkg"})
        result = _filter_commits_for_scope({sha}, project)
        assert sha in result


# ---------------------------------------------------------------------------
# check_coverage scoped to releasable
# ---------------------------------------------------------------------------


class TestCheckCoverageReleasable:
    """check_coverage works with a list of member projects."""

    def test_coverage_across_releasable_members(self, mock_git_repo):
        """Commits in any member project require coverage."""
        root = mock_git_repo
        (root / "pkg-a").mkdir()
        (root / "pkg-b").mkdir()

        sha_a = make_commit(root, "pkg-a/code.py", "change in a")
        sha_b = make_commit(root, "pkg-b/code.py", "change in b")
        sha_other = make_commit(root, "readme.txt", "top-level")

        projects = [
            WorkspaceProject({"name": "a", "path": "pkg-a"}),
            WorkspaceProject({"name": "b", "path": "pkg-b"}),
        ]

        # Only cover sha_a -- sha_b is missing
        entries = [
            ChangelogEntry(commits=[sha_a], user_facing=False),
        ]
        passed, details = check_coverage(entries, project=projects)
        # sha_b should be reported as uncovered; sha_other and the initial
        # commit are outside both projects and should be skipped
        uncovered_details = [d for d in details if "uncovered" in d or "not covered" in d]
        assert not passed, f"Expected fail. Details: {details}"
        assert len(uncovered_details) == 1, f"Expected 1 uncovered. Details: {details}"
        assert sha_b[:12] in uncovered_details[0]

    def test_full_coverage_passes(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg-a").mkdir()
        (root / "pkg-b").mkdir()

        sha_a = make_commit(root, "pkg-a/code.py", "change in a")
        sha_b = make_commit(root, "pkg-b/code.py", "change in b")

        projects = [
            WorkspaceProject({"name": "a", "path": "pkg-a"}),
            WorkspaceProject({"name": "b", "path": "pkg-b"}),
        ]

        entries = [
            ChangelogEntry(commits=[sha_a], user_facing=False),
            ChangelogEntry(commits=[sha_b], user_facing=False),
        ]
        passed, details = check_coverage(entries, project=projects)
        assert passed


# ---------------------------------------------------------------------------
# check_in_range with releasable members
# ---------------------------------------------------------------------------


class TestCheckInRangeReleasable:
    """check_in_range works with a list of member projects."""

    def test_in_range_with_member_projects(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]
        entries = [ChangelogEntry(commits=[sha], user_facing=False)]
        passed, details = check_in_range(entries, project=projects)
        assert passed


# ---------------------------------------------------------------------------
# check_no_orphans with releasable members
# ---------------------------------------------------------------------------


class TestCheckNoOrphansReleasable:
    """check_no_orphans works with a list of member projects."""

    def test_no_orphans_with_member_projects(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]
        entries = [ChangelogEntry(commits=[sha], user_facing=False)]
        passed, details = check_no_orphans(entries, project=projects)
        assert passed


# ---------------------------------------------------------------------------
# packages field on ChangelogEntry
# ---------------------------------------------------------------------------


class TestPackagesField:
    """Optional packages field on ChangelogEntry."""

    def test_default_is_none(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        assert entry.packages is None

    def test_packages_set(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False, packages=["a", "b"])
        assert entry.packages == ["a", "b"]

    def test_serialize_without_packages(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        s = serialize_entry(entry)
        assert "packages" not in s

    def test_serialize_with_packages(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False, packages=["a", "b"])
        s = serialize_entry(entry)
        assert '"packages":["a","b"]' in s

    def test_parse_without_packages(self):
        line = '{"commits":["abc"],"user_facing":false}'
        entry = parse_entry(line)
        assert entry.packages is None

    def test_parse_with_packages(self):
        line = '{"commits":["abc"],"user_facing":false,"packages":["a","b"]}'
        entry = parse_entry(line)
        assert entry.packages == ["a", "b"]

    def test_roundtrip(self):
        original = ChangelogEntry(commits=["abc"], user_facing=True,
                                  description="test", type="feature",
                                  packages=["pkg-a", "pkg-b"])
        s = serialize_entry(original)
        parsed = parse_entry(s)
        assert parsed.packages == ["pkg-a", "pkg-b"]
        assert parsed.description == "test"
        assert parsed.user_facing is True

    def test_validate_schema_valid_packages(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False,
                               packages=["a", "b"])
        errors = validate_schema(entry)
        assert not errors

    def test_validate_schema_none_packages_ok(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        errors = validate_schema(entry)
        assert not errors

    def test_validate_schema_invalid_packages_not_list(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        entry.packages = "not-a-list"
        errors = validate_schema(entry)
        assert any("packages must be a list" in e for e in errors)

    def test_validate_schema_invalid_packages_items(self):
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        entry.packages = [123, "ok"]
        errors = validate_schema(entry)
        assert any("packages must be a list of strings" in e for e in errors)

    def test_backward_compat_existing_entries(self):
        """Entries without packages field still parse and validate fine."""
        line = '{"commits":["abc"],"user_facing":true,"description":"test","type":"feature"}'
        entry = parse_entry(line)
        assert entry.packages is None
        errors = validate_schema(entry)
        assert not errors


# ---------------------------------------------------------------------------
# CHANGELOG.md generation per releasable
# ---------------------------------------------------------------------------


class TestGenerateChangelogPerReleasable:
    """generate_changelog with changes_dir_override and changelog_output_path."""

    def test_generate_to_releasable_dir(self, tmp_path):
        """CHANGELOG.md is written to the releasable directory."""
        # Set up a releasable changes dir
        rel_dir = os.path.join(str(tmp_path), ".rlsbl-monorepo", "releasables", "core")
        changes_dir = os.path.join(rel_dir, "changes")
        os.makedirs(changes_dir)

        # Write an entry
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Test feature",
            type="feature",
        )
        append_entry(changes_dir, entry)

        # Create project config
        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        # Generate
        changelog_path = os.path.join(rel_dir, "CHANGELOG.md")
        content = generate_changelog(
            str(tmp_path),
            changes_dir_override=changes_dir,
            changelog_output_path=changelog_path,
        )

        assert os.path.isfile(changelog_path)
        with open(changelog_path, "r") as f:
            written = f.read()
        assert "Test feature" in written
        assert content == written

    def test_default_behavior_unchanged(self, tmp_path):
        """Without overrides, behaves normally (writes to project root)."""
        changes_dir = os.path.join(str(tmp_path), ".rlsbl", "changes")
        os.makedirs(changes_dir)
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Normal feature",
            type="feature",
        )
        append_entry(changes_dir, entry)

        config_dir = tmp_path / ".rlsbl"
        (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        content = generate_changelog(str(tmp_path))
        changelog_path = os.path.join(str(tmp_path), "CHANGELOG.md")
        assert os.path.isfile(changelog_path)
        assert "Normal feature" in content

    def test_per_version_md_files_in_releasable_dir(self, tmp_path):
        """Per-version .md files are written alongside JSONL in changes_dir."""
        rel_dir = os.path.join(str(tmp_path), ".rlsbl-monorepo", "releasables", "core")
        changes_dir = os.path.join(rel_dir, "changes")
        os.makedirs(changes_dir)

        # Write a versioned JSONL file
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Released feature",
            type="feature",
        )
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        with open(unreleased_path, "w") as f:
            f.write(serialize_entry(entry) + "\n")

        # Also write a versioned file manually
        versioned_path = os.path.join(changes_dir, "1.0.0.jsonl")
        with open(versioned_path, "w") as f:
            f.write(serialize_entry(entry) + "\n")

        config_dir = tmp_path / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        changelog_path = os.path.join(rel_dir, "CHANGELOG.md")
        generate_changelog(
            str(tmp_path),
            changes_dir_override=changes_dir,
            changelog_output_path=changelog_path,
        )

        # Per-version md should be in changes_dir
        md_path = os.path.join(changes_dir, "1.0.0.md")
        assert os.path.isfile(md_path)


# ---------------------------------------------------------------------------
# Cache per releasable
# ---------------------------------------------------------------------------


class TestCachePerReleasable:
    """The .validated cache goes inside each releasable's changes dir."""

    def test_cache_path_in_releasable_dir(self, tmp_path):
        from rlsbl.changelog.validate import _cache_path
        changes_dir = get_releasable_changes_dir(str(tmp_path), "core")
        path = _cache_path(changes_dir)
        assert path == os.path.join(changes_dir, ".validated")
        assert "releasables/core/changes" in path

    def test_cache_isolation_between_releasables(self, tmp_path):
        """Two releasables have independent cache files."""
        from rlsbl.changelog.validate import _cache_path
        core_changes = get_releasable_changes_dir(str(tmp_path), "core")
        www_changes = get_releasable_changes_dir(str(tmp_path), "www")
        assert _cache_path(core_changes) != _cache_path(www_changes)

    def test_cache_written_after_validation(self, mock_git_repo):
        """validate_unreleased writes .validated to changes_dir."""
        from rlsbl.changelog.validate import validate_unreleased, _cache_path

        root = mock_git_repo
        rel_dir = os.path.join(str(root), ".rlsbl-monorepo", "releasables", "core")
        changes_dir = os.path.join(rel_dir, "changes")
        os.makedirs(changes_dir)

        # Write an unreleased.jsonl with a valid entry
        sha = make_commit(root, "code.py", "test change")
        entry = ChangelogEntry(commits=[sha], user_facing=True,
                               description="test", type="feature")
        append_entry(changes_dir, entry)

        config_dir = root / ".rlsbl"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        result = validate_unreleased(changes_dir, config={})
        if result["passed"]:
            cache_file = _cache_path(changes_dir)
            assert os.path.isfile(cache_file)
            assert "releasables/core/changes" in cache_file


# ---------------------------------------------------------------------------
# changelog add writes to releasable changes dir
# ---------------------------------------------------------------------------


class TestChangelogAddReleasable:
    """changelog add targets the releasable's unreleased.jsonl in explicit mode."""

    def test_resolve_changes_dir_explicit_mode(self, tmp_path, monkeypatch):
        """_resolve_changes_dir returns releasable changes dir in explicit mode."""
        from rlsbl.commands.changelog_cmd import _resolve_changes_dir, _ResolvedContext

        ws_root = str(tmp_path)
        releasable = Releasable(name="core")
        project = WorkspaceProject({"name": "a", "path": "a", "releasable": "core"})

        ctx = _ResolvedContext(
            project=project,
            releasable=releasable,
            ws_root=ws_root,
            member_projects=[project],
        )

        result = _resolve_changes_dir(ctx, str(tmp_path / "a"))
        expected = get_releasable_changes_dir(ws_root, "core")
        assert result == expected

    def test_resolve_changes_dir_no_releasable(self, tmp_path):
        """_resolve_changes_dir returns per-project changes dir without releasable."""
        from rlsbl.commands.changelog_cmd import _resolve_changes_dir, _ResolvedContext

        project = WorkspaceProject({"name": "a", "path": "a"})
        ctx = _ResolvedContext(project=project, releasable=None, ws_root=None)

        project_root = str(tmp_path / "a")
        result = _resolve_changes_dir(ctx, project_root)
        expected = get_changes_dir(project_root)
        assert result == expected

    def test_resolve_changes_dir_none_context(self, tmp_path):
        """_resolve_changes_dir with None context uses per-project path."""
        from rlsbl.commands.changelog_cmd import _resolve_changes_dir

        project_root = str(tmp_path / "standalone")
        result = _resolve_changes_dir(None, project_root)
        expected = get_changes_dir(project_root)
        assert result == expected


# ---------------------------------------------------------------------------
# derive_packages_from_commits
# ---------------------------------------------------------------------------


class TestDerivePackagesFromCommits:
    """_derive_packages_from_commits populates the packages field."""

    def test_derives_packages(self, mock_git_repo):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits

        root = mock_git_repo
        (root / "pkg-a").mkdir()
        (root / "pkg-b").mkdir()

        sha = make_commit(root, "pkg-a/code.py", "change in a")

        projects = [
            WorkspaceProject({"name": "a", "path": "pkg-a"}),
            WorkspaceProject({"name": "b", "path": "pkg-b"}),
        ]

        packages = _derive_packages_from_commits([sha], projects)
        assert packages == ["a"]

    def test_multiple_packages(self, mock_git_repo):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits

        root = mock_git_repo
        (root / "pkg-a").mkdir()
        (root / "pkg-b").mkdir()

        # Commit touching both projects (two files in one commit)
        (root / "pkg-a" / "code.py").write_text("content\n")
        (root / "pkg-b" / "code.py").write_text("content\n")
        run_git(root, "add", "pkg-a/code.py", "pkg-b/code.py")
        run_git(root, "commit", "-q", "-m", "cross-package change")
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=str(root), check=True,
        )
        sha = result.stdout.strip()

        projects = [
            WorkspaceProject({"name": "a", "path": "pkg-a"}),
            WorkspaceProject({"name": "b", "path": "pkg-b"}),
        ]

        packages = _derive_packages_from_commits([sha], projects)
        assert packages == ["a", "b"]

    def test_no_member_projects_returns_none(self):
        from rlsbl.commands.changelog_cmd import _derive_packages_from_commits
        result = _derive_packages_from_commits(["abc"], [])
        assert result is None


# ---------------------------------------------------------------------------
# Integration: end-to-end releasable changelog flow
# ---------------------------------------------------------------------------


class TestReleasableChangelogIntegration:
    """End-to-end tests combining multiple releasable changelog features."""

    def test_append_and_read_in_releasable_dir(self, tmp_path):
        """Entries appended to releasable changes dir are readable."""
        changes_dir = _setup_releasable_changes(tmp_path, "core")

        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="New feature",
            type="feature",
            packages=["pkg-a"],
        )
        append_entry(changes_dir, entry)

        entries = read_unreleased(changes_dir)
        assert len(entries) == 1
        assert entries[0].description == "New feature"
        assert entries[0].packages == ["pkg-a"]

    def test_finalize_in_releasable_dir(self, tmp_path):
        """finalize_version works with releasable changes dir."""
        changes_dir = _setup_releasable_changes(tmp_path, "core")

        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Feature",
            type="feature",
        )
        append_entry(changes_dir, entry)

        finalize_version(changes_dir, "1.0.0")

        # Versioned file should exist and be read-only
        versioned = os.path.join(changes_dir, "1.0.0.jsonl")
        assert os.path.isfile(versioned)
        import stat
        mode = os.stat(versioned).st_mode
        assert not (mode & stat.S_IWUSR)

        # New unreleased should be empty
        entries = read_unreleased(changes_dir)
        assert entries == []

    def test_read_all_versioned_entries_in_releasable_dir(self, tmp_path):
        """_read_all_versioned_entries works with releasable changes dirs."""
        changes_dir = _setup_releasable_changes(tmp_path, "core")

        # Write some entries and finalize
        entry1 = ChangelogEntry(commits=["a1"], user_facing=True,
                                description="v1 feature", type="feature")
        append_entry(changes_dir, entry1)
        finalize_version(changes_dir, "1.0.0")

        entry2 = ChangelogEntry(commits=["b2"], user_facing=True,
                                description="v2 fix", type="fix")
        append_entry(changes_dir, entry2)

        entries_by_version = _read_all_versioned_entries(changes_dir)
        assert "1.0.0" in entries_by_version
        assert "unreleased" in entries_by_version
        assert len(entries_by_version["1.0.0"]) == 1
        assert len(entries_by_version["unreleased"]) == 1

    def test_two_releasables_independent(self, tmp_path):
        """Two releasables have independent changes dirs and entries."""
        core_dir = _setup_releasable_changes(tmp_path, "core")
        www_dir = _setup_releasable_changes(tmp_path, "www")

        core_entry = ChangelogEntry(commits=["c1"], user_facing=True,
                                    description="Core feature", type="feature",
                                    packages=["core-lib"])
        www_entry = ChangelogEntry(commits=["w1"], user_facing=True,
                                   description="WWW fix", type="fix",
                                   packages=["web-app"])

        append_entry(core_dir, core_entry)
        append_entry(www_dir, www_entry)

        core_entries = read_unreleased(core_dir)
        www_entries = read_unreleased(www_dir)

        assert len(core_entries) == 1
        assert len(www_entries) == 1
        assert core_entries[0].description == "Core feature"
        assert www_entries[0].description == "WWW fix"
        assert core_entries[0].packages == ["core-lib"]
        assert www_entries[0].packages == ["web-app"]
