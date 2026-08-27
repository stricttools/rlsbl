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
    _read_all_versioned_entries,
    check_coverage,
    check_in_range,
    check_no_orphans,
)
from rlsbl.git_util import filter_commits_for_scope
from rlsbl.ownership import OwnershipScope
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
        resolved_dir, tag_glob, scope, entries = result
        assert resolved_dir == changes_dir
        # tag_glob should be derived from releasable's tag_format
        assert tag_glob == "core@v*"
        # the scope covers both member projects, and carries the whole
        # workspace member list attribution is resolved against
        assert isinstance(scope, OwnershipScope)
        assert scope.owned == {"a", "b"}
        assert {p.name for p in scope.members} == {"a", "b"}
        assert {p.name for p in scope.owned_members()} == {"a", "b"}

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
# filter_commits_for_scope over a releasable's members
# ---------------------------------------------------------------------------


ROOT_MEMBER = WorkspaceProject({"name": "root", "path": "."})


def _scope(members, all_members=None):
    return OwnershipScope.for_members(all_members or members, members)


class TestFilterCommitsForReleasable:
    """filter_commits_for_scope with a scope spanning multiple members."""

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

        result = filter_commits_for_scope(
            {sha_a, sha_b, sha_other}, _scope(projects), operation="test",
        )
        assert sha_a in result
        assert sha_b in result
        assert sha_other not in result

    def test_single_project_scope(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()

        sha = make_commit(root, "pkg/code.py", "change")
        sha_other = make_commit(root, "other.txt", "other")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]

        result = filter_commits_for_scope(
            {sha, sha_other}, _scope(projects), operation="test",
        )
        assert sha in result
        assert sha_other not in result

    def test_empty_projects_returns_empty(self, mock_git_repo):
        root = mock_git_repo
        sha = make_commit(root, "file.txt", "change")
        result = filter_commits_for_scope({sha}, _scope([]), operation="test")
        assert len(result) == 0

    def test_watch_globs_no_longer_claim_files(self, mock_git_repo):
        """A watch glob is not a territory claim -- the root member owns the file.

        Attribution accepted watch globs as a second claim path, so two members
        could own the same file. Ownership now comes from declared paths alone.
        """
        root = mock_git_repo
        (root / "pkg").mkdir()
        (root / "shared").mkdir()

        sha = make_commit(root, "shared/config.json", "config change")

        watcher = WorkspaceProject({"name": "p", "path": "pkg", "watch": ["shared/*"]})
        all_members = [ROOT_MEMBER, watcher]

        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(all_members, watcher), operation="test",
        ) == set()
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(all_members, ROOT_MEMBER), operation="test",
        ) == {sha}


# ---------------------------------------------------------------------------
# scope handling in the validation entry points
# ---------------------------------------------------------------------------


class TestFilterCommitsForScope:
    """filter_commits_for_scope handles the None and member-list cases."""

    def test_none_scope_returns_unchanged(self):
        commits = {"aaa", "bbb", "ccc"}
        result = filter_commits_for_scope(commits, None, operation="test")
        assert result == commits

    def test_member_list_scope(self, mock_git_repo, monkeypatch):
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]
        result = filter_commits_for_scope({sha}, _scope(projects), operation="test")
        assert sha in result

    def test_single_member_scope(self, mock_git_repo):
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/code.py", "change")

        project = WorkspaceProject({"name": "p", "path": "pkg"})
        result = filter_commits_for_scope(
            {sha}, OwnershipScope.for_member([project], project), operation="test",
        )
        assert sha in result
