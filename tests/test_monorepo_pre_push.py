"""Tests for monorepo-aware pre-push helper functions."""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conftest import run_git, git_head, make_commit, make_workspace as _make_workspace
from rlsbl.prepush_utils import _parse_stdin_refs
from rlsbl.git_util import (
    get_commit_files,
    file_matches_project,
    filter_commits_for_project,
    affected_projects,
)


# -- Unit tests for helpers ---------------------------------------------------


class TestFileMatchesProject:
    def test_file_inside_project_path(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert file_matches_project("pkg-a/src/main.js", proj)

    def test_file_is_project_root(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert file_matches_project("pkg-a", proj)

    def test_file_outside_project(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert not file_matches_project("pkg-b/src/main.js", proj)

    def test_file_with_similar_prefix(self):
        """pkg-ab should not match pkg-a (no partial prefix match)."""
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert not file_matches_project("pkg-ab/src/main.js", proj)

    def test_watch_glob_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert file_matches_project("shared/utils.js", proj)

    def test_watch_exact_file_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["Package.swift"]}
        assert file_matches_project("Package.swift", proj)

    def test_watch_no_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert not file_matches_project("other/file.js", proj)


class TestAffectedProjects:
    def test_single_project_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js"}
        result = affected_projects(changed, projects)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_both_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js", "pkg-b/lib/index.js"}
        result = affected_projects(changed, projects)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"a", "b"}

    def test_no_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"README.md", "docs/guide.md"}
        result = affected_projects(changed, projects)
        assert result == []

    def test_watch_triggers_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a", "watch": ["shared/**"]},
        ]
        changed = {"shared/utils.js"}
        result = affected_projects(changed, projects)
        assert len(result) == 1
        assert result[0]["name"] == "a"


class TestParseStdinRefs:
    def test_normal_input(self):
        stdin_data = "refs/heads/main abc123 refs/heads/main def456\n"
        with patch("sys.stdin", StringIO(stdin_data)):
            with patch("sys.stdin.isatty", return_value=False):
                refs = _parse_stdin_refs()
        assert refs == [("abc123", "def456")]

    def test_multiple_refs(self):
        stdin_data = (
            "refs/heads/main abc123 refs/heads/main def456\n"
            "refs/heads/feat xyz789 refs/heads/feat 000111\n"
        )
        with patch("sys.stdin", StringIO(stdin_data)):
            with patch("sys.stdin.isatty", return_value=False):
                refs = _parse_stdin_refs()
        assert len(refs) == 2

    def test_empty_stdin(self):
        with patch("sys.stdin", StringIO("")):
            with patch("sys.stdin.isatty", return_value=False):
                refs = _parse_stdin_refs()
        assert refs is None

    def test_tty_stdin(self):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        with patch("sys.stdin", mock_stdin):
            refs = _parse_stdin_refs()
        assert refs is None


# -- Tests for path-based commit filtering -----------------------------------


class TestGetCommitFiles:
    """Unit tests for get_commit_files."""

    def test_returns_files_for_commit(self, monorepo_fixture):
        """Returns the list of files changed by a commit."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "add go file")
        files = get_commit_files(sha)
        assert files == ["go/main.go"]

    def test_returns_multiple_files(self, monorepo_fixture):
        """Returns all files when a commit touches multiple."""
        root = monorepo_fixture.root
        (root / "python" / "lib.py").write_text("# lib\n")
        (root / "go" / "main.go").write_text("// main\n")
        run_git(root, "add", "python/lib.py", "go/main.go")
        run_git(root, "commit", "-q", "-m", "cross-project change")
        sha = git_head(root)
        files = get_commit_files(sha)
        assert sorted(files) == ["go/main.go", "python/lib.py"]

    def test_returns_files_for_merge_commit(self, monorepo_fixture):
        """Returns files introduced by a merge commit (first-parent diff)."""
        root = monorepo_fixture.root
        # Create a feature branch with a new file
        run_git(root, "checkout", "-b", "feature")
        sha_feature = make_commit(root, "go/feature.go", "feature work")
        # Switch back to main and make a diverging commit
        run_git(root, "checkout", "main")
        make_commit(root, "python/main_work.py", "main work")
        # Merge feature into main (creates a merge commit)
        run_git(root, "merge", "--no-ff", "feature", "-m", "merge feature")
        merge_sha = git_head(root)
        files = get_commit_files(merge_sha)
        # The merge introduced go/feature.go to mainline
        assert files is not None
        assert "go/feature.go" in files


class TestFilterCommitsForProject:
    """Unit tests for filter_commits_for_project."""

    def test_go_only_commit_not_in_python(self, monorepo_fixture):
        """A commit touching only go/ files is NOT included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        result = filter_commits_for_project({sha}, python_proj)
        assert result == set()

    def test_go_only_commit_in_go(self, monorepo_fixture):
        """A commit touching only go/ files IS included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        result = filter_commits_for_project({sha}, go_proj)
        assert result == {sha}

    def test_cross_project_commit_in_both(self, monorepo_fixture):
        """A commit touching both python/ and go/ is included for both projects."""
        root = monorepo_fixture.root
        (root / "python" / "lib.py").write_text("# lib\n")
        (root / "go" / "main.go").write_text("// main\n")
        run_git(root, "add", "python/lib.py", "go/main.go")
        run_git(root, "commit", "-q", "-m", "cross-project")
        sha = git_head(root)

        python_proj = {"path": "python", "name": "mypylib"}
        go_proj = {"path": "go", "name": "mygolib"}
        assert filter_commits_for_project({sha}, python_proj) == {sha}
        assert filter_commits_for_project({sha}, go_proj) == {sha}

    def test_python_only_commit_not_in_go(self, monorepo_fixture):
        """A commit touching only python/ files is NOT included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        result = filter_commits_for_project({sha}, go_proj)
        assert result == set()

    def test_python_only_commit_in_python(self, monorepo_fixture):
        """A commit touching only python/ files IS included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        result = filter_commits_for_project({sha}, python_proj)
        assert result == {sha}
