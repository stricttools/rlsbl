"""Tests for monorepo-aware pre-push changelog check."""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conftest import run_git, git_head, make_commit, make_workspace as _make_workspace
from rlsbl.commands.pre_push_check import (
    run_cmd,
    _detect_version,
    _parse_stdin_refs,
    _get_changed_files,
    _get_commit_files,
    _file_matches_project,
    _filter_commits_for_project,
    _affected_projects,
    _run_monorepo_check,
)
from rlsbl.context import ProjectContext


def _make_npm_project(root, subdir, version="1.0.0", changelog_version=None):
    """Create a minimal npm project with package.json and CHANGELOG.md."""
    proj_dir = root / subdir
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "package.json").write_text(
        json.dumps({"name": f"test-{subdir}", "version": version})
    )
    if changelog_version is not None:
        (proj_dir / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {changelog_version}\n\n- Changes\n"
        )


def _stdin_line(local_sha="abc1234", remote_sha="def5678"):
    """Build a single pre-push stdin line."""
    return f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"


# -- Unit tests for helpers ---------------------------------------------------


class TestFileMatchesProject:
    def test_file_inside_project_path(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert _file_matches_project("pkg-a/src/main.js", proj)

    def test_file_is_project_root(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert _file_matches_project("pkg-a", proj)

    def test_file_outside_project(self):
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert not _file_matches_project("pkg-b/src/main.js", proj)

    def test_file_with_similar_prefix(self):
        """pkg-ab should not match pkg-a (no partial prefix match)."""
        proj = {"path": "pkg-a", "name": "pkg-a"}
        assert not _file_matches_project("pkg-ab/src/main.js", proj)

    def test_watch_glob_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert _file_matches_project("shared/utils.js", proj)

    def test_watch_exact_file_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["Package.swift"]}
        assert _file_matches_project("Package.swift", proj)

    def test_watch_no_match(self):
        proj = {"path": "pkg-a", "name": "pkg-a", "watch": ["shared/**"]}
        assert not _file_matches_project("other/file.js", proj)


class TestAffectedProjects:
    def test_single_project_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js"}
        result = _affected_projects(changed, projects)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_both_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"pkg-a/src/main.js", "pkg-b/lib/index.js"}
        result = _affected_projects(changed, projects)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"a", "b"}

    def test_no_projects_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a"},
            {"path": "pkg-b", "name": "b"},
        ]
        changed = {"README.md", "docs/guide.md"}
        result = _affected_projects(changed, projects)
        assert result == []

    def test_watch_triggers_affected(self):
        projects = [
            {"path": "pkg-a", "name": "a", "watch": ["shared/**"]},
        ]
        changed = {"shared/utils.js"}
        result = _affected_projects(changed, projects)
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


# -- Integration tests for run_cmd (single-project, no monorepo) -------------


class TestSingleProjectWithoutJsonl:
    """Without .rlsbl/changes/, pre-push warns and exits 0."""

    def test_exits_zero_warns_no_jsonl(self, tmp_project, capsys):
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"})
        )
        (tmp_project / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Initial\n")
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "JSONL changelog not set up" in captured.err

    def test_exits_zero_with_no_project(self, tmp_project):
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
        assert exc_info.value.code == 0


# -- Integration tests for monorepo pre-push ---------------------------------


class TestMonorepoPrePush:
    """Test monorepo-aware pre-push changelog checking."""

    def _setup_monorepo(self, root, projects_config):
        """Set up workspace + project directories.

        projects_config: list of dicts with keys:
            name, path, version, changelog_version (None to skip changelog),
            watch (optional list of globs)
        """
        ws_projects = []
        for pc in projects_config:
            ws_entry = {"path": pc["path"], "name": pc["name"]}
            if "watch" in pc:
                ws_entry["watch"] = pc["watch"]
            ws_projects.append(ws_entry)

            _make_npm_project(
                root, pc["path"],
                version=pc.get("version", "1.0.0"),
                changelog_version=pc.get("changelog_version"),
            )
        _make_workspace(root, ws_projects)

    def test_touching_project_a_checks_a(self, tmp_project, capsys):
        """Push touching project A checks A's changelog."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
            {"name": "b", "path": "pkg-b", "version": "2.0.0", "changelog_version": "2.0.0"},
        ])

        changed_files = {"pkg-a/src/main.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_touching_a_and_b_checks_both(self, tmp_project, capsys):
        """Push touching A and B checks both changelogs."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
            {"name": "b", "path": "pkg-b", "version": "2.0.0", "changelog_version": "2.0.0"},
        ])

        changed_files = {"pkg-a/src/main.js", "pkg-b/lib/index.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_touching_a_does_not_check_b(self, tmp_project, capsys):
        """Push touching only A does NOT check B's changelog (even if B's is wrong)."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
            {"name": "b", "path": "pkg-b", "version": "2.0.0", "changelog_version": "0.0.1"},
        ])

        changed_files = {"pkg-a/src/main.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            # Should pass because B is not checked
            assert exc_info.value.code == 0

    def test_missing_changelog_skipped(self, tmp_project, capsys):
        """Project with no changelog is skipped silently."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": None},
        ])

        changed_files = {"pkg-a/src/main.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_all_changelogs_valid_exits_zero(self, tmp_project, capsys):
        """All affected projects have valid changelogs -- exit 0."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
            {"name": "b", "path": "pkg-b", "version": "2.0.0", "changelog_version": "2.0.0"},
            {"name": "c", "path": "pkg-c", "version": "3.0.0", "changelog_version": "3.0.0"},
        ])

        changed_files = {"pkg-a/x.js", "pkg-b/y.js", "pkg-c/z.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_projects_without_jsonl_skipped(self, tmp_project, capsys):
        """Projects without .rlsbl/changes/ are skipped in monorepo pre-push."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
            {"name": "b", "path": "pkg-b", "version": "2.0.0", "changelog_version": "0.0.1"},
        ])

        # Neither project has .rlsbl/changes/, so both are skipped
        changed_files = {"pkg-a/x.js", "pkg-b/y.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_no_stdin_falls_back_to_single_project(self, tmp_project, capsys):
        """When stdin is empty (e.g., called directly), falls back to single-project."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "1.0.0"},
        ])
        # Also put a top-level package.json so single-project logic finds it
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "mono-root", "version": "0.1.0"})
        )
        (tmp_project / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n\n- Root\n")

        # Simulate tty stdin (no hook data)
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        with patch("sys.stdin", mock_stdin):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            assert exc_info.value.code == 0

    def test_watch_glob_triggers_check(self, tmp_project, capsys):
        """A change in a watched path triggers the project's changelog check.

        Without .rlsbl/changes/, the project is skipped (exits 0).
        """
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0",
             "changelog_version": "0.0.1", "watch": ["shared/**"]},
        ])
        # Also create the watched directory
        (tmp_project / "shared").mkdir()
        (tmp_project / "shared" / "utils.js").write_text("// shared")

        changed_files = {"shared/utils.js"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            # Project has no JSONL changelog, so it's skipped
            assert exc_info.value.code == 0

    def test_files_not_in_any_project_ignored(self, tmp_project, capsys):
        """Files outside all project paths are ignored."""
        self._setup_monorepo(tmp_project, [
            {"name": "a", "path": "pkg-a", "version": "1.0.0", "changelog_version": "0.0.1"},
        ])

        # Only root-level files changed, not in any project
        changed_files = {"README.md", "docs/guide.md"}
        stdin_data = _stdin_line()

        with patch("sys.stdin", StringIO(stdin_data)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("rlsbl.commands.pre_push_check._get_changed_files",
                   return_value=changed_files):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={}))
            # No projects affected, so exit 0
            assert exc_info.value.code == 0


# -- Tests for path-based commit filtering -----------------------------------


class TestGetCommitFiles:
    """Unit tests for _get_commit_files."""

    def test_returns_files_for_commit(self, monorepo_fixture):
        """Returns the list of files changed by a commit."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "add go file")
        files = _get_commit_files(sha)
        assert files == ["go/main.go"]

    def test_returns_multiple_files(self, monorepo_fixture):
        """Returns all files when a commit touches multiple."""
        root = monorepo_fixture.root
        (root / "python" / "lib.py").write_text("# lib\n")
        (root / "go" / "main.go").write_text("// main\n")
        run_git(root, "add", "python/lib.py", "go/main.go")
        run_git(root, "commit", "-q", "-m", "cross-project change")
        sha = git_head(root)
        files = _get_commit_files(sha)
        assert sorted(files) == ["go/main.go", "python/lib.py"]


class TestFilterCommitsForProject:
    """Unit tests for _filter_commits_for_project."""

    def test_go_only_commit_not_in_python(self, monorepo_fixture):
        """A commit touching only go/ files is NOT included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        result = _filter_commits_for_project({sha}, python_proj)
        assert result == set()

    def test_go_only_commit_in_go(self, monorepo_fixture):
        """A commit touching only go/ files IS included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "go/main.go", "go-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        result = _filter_commits_for_project({sha}, go_proj)
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
        assert _filter_commits_for_project({sha}, python_proj) == {sha}
        assert _filter_commits_for_project({sha}, go_proj) == {sha}

    def test_python_only_commit_not_in_go(self, monorepo_fixture):
        """A commit touching only python/ files is NOT included for the go project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        go_proj = {"path": "go", "name": "mygolib"}
        result = _filter_commits_for_project({sha}, go_proj)
        assert result == set()

    def test_python_only_commit_in_python(self, monorepo_fixture):
        """A commit touching only python/ files IS included for the python project."""
        root = monorepo_fixture.root
        sha = make_commit(root, "python/app.py", "python-only change")

        python_proj = {"path": "python", "name": "mypylib"}
        result = _filter_commits_for_project({sha}, python_proj)
        assert result == {sha}


class TestMonorepoPathFiltering:
    """Integration tests: monorepo pre-push check filters commits per project."""

    def _write_jsonl_entry(self, proj_dir, commits, user_facing=False):
        """Append a JSONL entry to a project's unreleased.jsonl."""
        changes_dir = proj_dir / ".rlsbl" / "changes"
        entry = {"commits": commits, "user_facing": user_facing}
        if user_facing:
            entry["description"] = "Test change."
            entry["type"] = "feature"
        with open(changes_dir / "unreleased.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def test_go_only_commit_does_not_require_python_coverage(self, monorepo_fixture, capsys):
        """A commit touching only go/ does NOT need coverage in python's changelog."""
        root = monorepo_fixture.root
        projects = monorepo_fixture.projects

        # Make a commit that only touches go/
        go_sha = make_commit(root, "go/main.go", "go-only change")

        # Add coverage in go's changelog, but NOT in python's
        self._write_jsonl_entry(monorepo_fixture.go_dir, [go_sha[:12]])
        run_git(root, "add", "go/.rlsbl/changes/unreleased.jsonl")
        run_git(root, "commit", "-q", "-m", "changelog: go entry")

        # Changed files span only go/
        changed_files = {"go/main.go"}

        # The pushed commits include both the go change and the changelog commit
        # We mock _get_pushed_commits to return just the go commit
        # (changelog commits are release infra and get filtered out anyway)
        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={go_sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            assert exc_info.value.code == 0

    def test_cross_project_commit_requires_both_changelogs(self, monorepo_fixture, capsys):
        """A commit touching both python/ and go/ requires coverage in BOTH changelogs."""
        root = monorepo_fixture.root
        projects = monorepo_fixture.projects

        # Make a cross-project commit
        (root / "python" / "lib.py").write_text("# lib\n")
        (root / "go" / "main.go").write_text("// main\n")
        run_git(root, "add", "python/lib.py", "go/main.go")
        run_git(root, "commit", "-q", "-m", "cross-project change")
        cross_sha = git_head(root)

        # Add coverage ONLY in go's changelog
        self._write_jsonl_entry(monorepo_fixture.go_dir, [cross_sha[:12]])
        run_git(root, "add", "go/.rlsbl/changes/unreleased.jsonl")
        run_git(root, "commit", "-q", "-m", "changelog: go entry")

        # Changed files span both projects
        changed_files = {"python/lib.py", "go/main.go"}

        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={cross_sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            # Should fail because python's changelog is missing coverage
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "mypylib" in captured.err

    def test_python_only_commit_requires_only_python_coverage(self, monorepo_fixture, capsys):
        """A commit touching only python/ requires coverage only in python's changelog."""
        root = monorepo_fixture.root
        projects = monorepo_fixture.projects

        # Make a python-only commit
        py_sha = make_commit(root, "python/app.py", "python-only change")

        # Add coverage ONLY in python's changelog
        self._write_jsonl_entry(monorepo_fixture.python_dir, [py_sha[:12]])
        run_git(root, "add", "python/.rlsbl/changes/unreleased.jsonl")
        run_git(root, "commit", "-q", "-m", "changelog: python entry")

        # Changed files span only python/
        changed_files = {"python/app.py"}

        with patch("rlsbl.commands.pre_push_check._get_pushed_commits",
                   return_value={py_sha}):
            with pytest.raises(SystemExit) as exc_info:
                _run_monorepo_check(str(root), projects, changed_files, refs=[("fake", "fake")])
            # Should pass -- python has coverage, go is not checked
            assert exc_info.value.code == 0
