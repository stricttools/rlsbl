"""Tests for filter_commits_for_scope over a multi-member scope.

Verifies that a scope covering several members includes exactly the commits
touching a file one of those members owns, with attribution resolved against
the whole workspace member list.
"""

import pytest

from conftest import make_commit
from rlsbl.git_util import filter_commits_for_scope
from rlsbl.ownership import OwnershipError, OwnershipScope
from rlsbl.workspace import WorkspaceProject

ROOT = WorkspaceProject({"name": "root", "path": "."})


def scope(members, all_members=None):
    return OwnershipScope.for_members(all_members or members, members)


class TestFilterCommitsForScopeMultipleMembers:
    """filter_commits_for_scope with a scope over several members."""

    def test_only_commits_within_member_paths_returned(self, mock_git_repo):
        """Commits touching files inside member projects are included."""
        root = mock_git_repo
        (root / "lib-a").mkdir()
        (root / "lib-b").mkdir()

        sha_a = make_commit(root, "lib-a/mod.py", "change in lib-a")
        sha_b = make_commit(root, "lib-b/mod.py", "change in lib-b")

        projects = [
            WorkspaceProject({"name": "a", "path": "lib-a"}),
            WorkspaceProject({"name": "b", "path": "lib-b"}),
        ]

        result = filter_commits_for_scope(
            {sha_a, sha_b}, scope(projects), operation="test",
        )
        assert sha_a in result
        assert sha_b in result

    def test_commits_outside_all_member_paths_excluded(self, mock_git_repo):
        """Commits touching only files outside all member projects are excluded."""
        root = mock_git_repo
        (root / "lib-a").mkdir()
        (root / "lib-b").mkdir()

        sha_inside = make_commit(root, "lib-a/mod.py", "inside change")
        (root / "unrelated").mkdir()
        sha_outside = make_commit(root, "unrelated/stuff.txt", "outside change")

        projects = [
            WorkspaceProject({"name": "a", "path": "lib-a"}),
            WorkspaceProject({"name": "b", "path": "lib-b"}),
        ]

        result = filter_commits_for_scope(
            {sha_inside, sha_outside}, scope(projects), operation="test",
        )
        assert sha_inside in result
        assert sha_outside not in result

    def test_outside_commit_belongs_to_the_root_member(self, mock_git_repo):
        """The residual is not nobody's: the root member owns it."""
        root = mock_git_repo
        (root / "lib-a").mkdir()
        (root / "unrelated").mkdir()
        sha_outside = make_commit(root, "unrelated/stuff.txt", "outside change")

        member = WorkspaceProject({"name": "a", "path": "lib-a"})
        all_members = [ROOT, member]

        assert filter_commits_for_scope(
            {sha_outside},
            OwnershipScope.for_member(all_members, ROOT),
            operation="test",
        ) == {sha_outside}
        assert filter_commits_for_scope(
            {sha_outside},
            OwnershipScope.for_member(all_members, member),
            operation="test",
        ) == set()

    def test_commit_touching_one_member_included(self, mock_git_repo):
        """A commit touching files in only one member project is included."""
        root = mock_git_repo
        (root / "pkg-x").mkdir()
        (root / "pkg-y").mkdir()

        sha = make_commit(root, "pkg-y/code.py", "only in y")

        projects = [
            WorkspaceProject({"name": "x", "path": "pkg-x"}),
            WorkspaceProject({"name": "y", "path": "pkg-y"}),
        ]

        result = filter_commits_for_scope({sha}, scope(projects), operation="test")
        assert sha in result

    def test_empty_scope_returns_no_commits(self, mock_git_repo):
        """An empty member scope owns nothing, so no commits pass."""
        root = mock_git_repo
        sha = make_commit(root, "anything.txt", "some change")

        result = filter_commits_for_scope({sha}, scope([]), operation="test")
        assert len(result) == 0

    def test_none_scope_passes_everything(self, mock_git_repo):
        """No workspace means no scoping."""
        root = mock_git_repo
        sha = make_commit(root, "anything.txt", "some change")

        assert filter_commits_for_scope({sha}, None, operation="test") == {sha}

    def test_nested_member_owns_alone(self, mock_git_repo):
        """Most specific wins: the parent member does not also claim the file."""
        root = mock_git_repo
        (root / "pkg").mkdir()
        (root / "pkg" / "inner").mkdir()
        sha = make_commit(root, "pkg/inner/code.py", "nested change")

        outer = WorkspaceProject({"name": "outer", "path": "pkg"})
        inner = WorkspaceProject({"name": "inner", "path": "pkg/inner"})
        all_members = [ROOT, outer, inner]

        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(all_members, inner), operation="test",
        ) == {sha}
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(all_members, outer), operation="test",
        ) == set()

    def test_undeterminable_commit_is_a_hard_error(self, mock_git_repo):
        """A git read that cannot answer never becomes a silent include."""
        from unittest.mock import patch

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]
        with patch("rlsbl.git_util.get_commit_files", return_value=None):
            with pytest.raises(OwnershipError) as exc:
                filter_commits_for_scope(
                    {"cafebabe"}, scope(projects), operation="an example operation",
                )
        assert "cafebabe" in str(exc.value)
        assert "an example operation" in str(exc.value)
