"""Tests for monorepo-aware pre-push helper functions."""

import os
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest

from conftest import run_git, git_head, make_commit
from rlsbl.prepush_utils import _parse_stdin_refs
from rlsbl.git_util import (
    get_commit_files,
    affected_members,
    filter_commits_for_scope,
)
from rlsbl.ownership import OwnershipScope, owner_name_of


# -- Unit tests for helpers ---------------------------------------------------


ROOT = {"path": ".", "name": "root"}


def scope_for(proj, *others):
    """Ownership scope over one member, with the root member as residual owner."""
    members = [ROOT, proj, *others]
    return OwnershipScope.for_member(members, proj)


class TestFileOwnership:
    """Attribution is single-owner: the most specific declared path wins."""

    def test_file_inside_project_path(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert owner_name_of("pkg-a/src/main.js", [ROOT, proj]) == "pkg-a"

    def test_file_is_project_root(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert owner_name_of("pkg-a", [ROOT, proj]) == "pkg-a"

    def test_file_outside_project(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert owner_name_of("pkg-b/src/main.js", [ROOT, proj]) == "root"

    def test_file_with_similar_prefix(self):
        """pkg-ab should not match pkg-a (no partial prefix match)."""
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert owner_name_of("pkg-ab/src/main.js", [ROOT, proj]) == "root"

    def test_watch_glob_does_not_grant_ownership(self):
        """A watch glob is not a territory claim -- the owner is the root member.

        Attribution used to accept watch globs as a second way to claim a file,
        which let two members claim the same path. Ownership is decided by
        declared paths alone.
        """
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert owner_name_of("shared/utils.js", [ROOT, proj]) == "root"

    def test_watch_exact_file_does_not_grant_ownership(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["Package.swift"]}
        assert owner_name_of("Package.swift", [ROOT, proj]) == "root"

    def test_unwatched_file_still_belongs_to_the_root_member(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert owner_name_of("other/file.js", [ROOT, proj]) == "root"


class TestAffectedMembers:
    def test_single_project_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js"}
        result = affected_members(changed, projects)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_both_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js", "pkg-b/lib/index.js"}
        result = affected_members(changed, projects)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"a", "b"}

    def test_no_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"README.md", "docs/guide.md"}
        result = affected_members(changed, projects)
        assert result == []

    def test_root_member_takes_the_residual(self):
        """With a root member declared, root files affect it -- and only it."""
        projects = [ROOT, {"path": "pkg-a", "name": "a"}]
        result = affected_members({"README.md", "docs/guide.md"}, projects)
        assert [p["name"] for p in result] == ["root"]

    def test_watch_does_not_make_a_member_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a", "watch": ["shared/**"]},
        ]
        assert affected_members({"shared/utils.js"}, projects) == []

    def test_nested_member_claims_alone(self):
        projects = [
            {"path": "pkg", "name": "outer"},
            {"path": "pkg/inner", "name": "inner"},
        ]
        result = affected_members({"pkg/inner/a.py"}, projects)
        assert [p["name"] for p in result] == ["inner"]


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

    def test_returns_files_for_the_root_commit(self, tmp_path):
        """A repo's FIRST commit has no parent -- it still changed files.

        ``git diff-tree`` emits nothing for a parentless commit unless
        ``--root`` is passed, which made every root commit look like it
        touched no project at all.
        """
        repo = tmp_path / "fresh"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")
        (repo / "python").mkdir()
        (repo / "python" / "pyproject.toml").write_text(
            '[project]\nname = "pylib"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "python/pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "initial")

        os.chdir(repo)
        files = get_commit_files(git_head(repo))
        assert files == ["python/pyproject.toml"]

    def test_returns_files_for_merge_commit(self, monorepo_fixture):
        """Returns files introduced by a merge commit (first-parent diff)."""
        root = monorepo_fixture.root
        # Create a feature branch with a new file
        run_git(root, "checkout", "-b", "feature")
        make_commit(root, "go/feature.go", "feature work")
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


class TestFilterCommitsForScope:
    """Unit tests for filter_commits_for_scope."""

    def test_go_only_commit_not_in_python(self, monorepo_fixture):
        """A commit touching only go/ files is NOT included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        go_proj = {"path": "go", "name": "mygolib"}
        result = filter_commits_for_scope(
            {sha}, scope_for(python_proj, go_proj), operation="test",
        )
        assert result == set()

    def test_go_only_commit_in_go(self, monorepo_fixture):
        """A commit touching only go/ files IS included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        result = filter_commits_for_scope({sha}, scope_for(go_proj), operation="test")
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
        assert filter_commits_for_scope(
            {sha}, scope_for(python_proj, go_proj), operation="test",
        ) == {sha}
        assert filter_commits_for_scope(
            {sha}, scope_for(go_proj, python_proj), operation="test",
        ) == {sha}

    def test_python_only_commit_not_in_go(self, monorepo_fixture):
        """A commit touching only python/ files is NOT included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        python_proj = {"path": "python", "name": "mypylib"}
        result = filter_commits_for_scope(
            {sha}, scope_for(go_proj, python_proj), operation="test",
        )
        assert result == set()

    def test_python_only_commit_in_python(self, monorepo_fixture):
        """A commit touching only python/ files IS included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        result = filter_commits_for_scope(
            {sha}, scope_for(python_proj), operation="test",
        )
        assert result == {sha}

    def test_root_file_commit_belongs_to_the_root_member(self, monorepo_fixture):
        """The defect this replaces: a root member matched nothing at all.

        A ``path = "."`` member computed a ``"./"`` prefix, which no git path
        ever starts with, so its coverage was silently vacuous.
        """
        root = monorepo_fixture.root
        sha = make_commit(root, "README.md", "root-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        members = [ROOT, python_proj]
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(members, ROOT), operation="test",
        ) == {sha}
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(members, python_proj), operation="test",
        ) == set()

    def test_undeterminable_commit_is_a_hard_error(self, monorepo_fixture):
        """A git read that cannot answer is never a silent include or skip."""
        from rlsbl.ownership import OwnershipError

        proj = {"path": "python", "name": "mypylib"}
        with patch("rlsbl.git_util.get_commit_files", return_value=None):
            with pytest.raises(OwnershipError) as exc:
                filter_commits_for_scope(
                    {"deadbeef"}, scope_for(proj), operation="the test operation",
                )
        assert "deadbeef" in str(exc.value)
        assert "the test operation" in str(exc.value)
