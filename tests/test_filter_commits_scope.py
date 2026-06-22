"""Tests for _filter_commits_for_scope list dispatch.

Verifies that when ``project`` is a list of WorkspaceProject-like dicts,
the function delegates to ``filter_commits_for_releasable`` (not
``filter_commits_for_project``), correctly filtering commits by the
combined paths of all member projects.
"""

from unittest.mock import patch

from conftest import make_commit, run_git
from rlsbl.changelog.validate import _filter_commits_for_scope
from rlsbl.git_util import filter_commits_for_releasable
from rlsbl.workspace import WorkspaceProject


class TestFilterCommitsForScopeListDispatch:
    """_filter_commits_for_scope with a list of projects."""

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

        result = _filter_commits_for_scope({sha_a, sha_b}, projects)
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

        result = _filter_commits_for_scope({sha_inside, sha_outside}, projects)
        assert sha_inside in result
        assert sha_outside not in result

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

        result = _filter_commits_for_scope({sha}, projects)
        assert sha in result

    def test_empty_list_returns_no_commits(self, mock_git_repo):
        """An empty project list means no paths match, so no commits pass."""
        root = mock_git_repo
        sha = make_commit(root, "anything.txt", "some change")

        result = _filter_commits_for_scope({sha}, [])
        assert len(result) == 0

    def test_delegates_to_filter_commits_for_releasable(self, mock_git_repo):
        """When project is a list, filter_commits_for_releasable is called
        (not filter_commits_for_project)."""
        root = mock_git_repo
        (root / "pkg").mkdir()
        sha = make_commit(root, "pkg/f.py", "change")

        projects = [WorkspaceProject({"name": "p", "path": "pkg"})]

        with patch(
            "rlsbl.changelog.validate.filter_commits_for_releasable",
            wraps=filter_commits_for_releasable,
        ) as mock_releasable, patch(
            "rlsbl.changelog.validate.filter_commits_for_project",
        ) as mock_project:
            _filter_commits_for_scope({sha}, projects)
            mock_releasable.assert_called_once()
            mock_project.assert_not_called()
