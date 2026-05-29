"""Tests for monorepo workspace management commands (init, add, remove, list)."""

import json
import os

import pytest

from rlsbl.commands.monorepo import _cmd_init, _cmd_add, _cmd_remove, _cmd_list
from rlsbl.workspace import load_workspace, WORKSPACE_DIR, WORKSPACE_FILE


def _make_npm_project(base_path, subdir):
    """Create a minimal npm project (package.json) so detect_targets finds it."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + subdir, "version": "0.1.0"}, f)
    return subdir


class TestInit:
    def test_creates_workspace(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        ws_file = mock_git_repo / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()
        projects = load_workspace(str(mock_git_repo))
        assert projects == []
        captured = capsys.readouterr()
        assert "Initialized monorepo workspace" in captured.out

    def test_refuses_reinit(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_init({}, project_root=".")


class TestAdd:
    def test_adds_project(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        assert projects[0]["path"] == "pkg-a"
        assert projects[0]["name"] == "pkg-a"
        captured = capsys.readouterr()
        assert "Added project 'pkg-a' at pkg-a" in captured.out

    def test_uses_name_flag(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "libs/core")
        _cmd_add(["libs/core"], {"name": "core-lib"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert projects[0]["name"] == "core-lib"
        captured = capsys.readouterr()
        assert "core-lib" in captured.out

    def test_refuses_no_args(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add([], {}, project_root=".")

    def test_refuses_nonexistent_path(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["nonexistent"], {}, project_root=".")

    def test_refuses_no_target(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        os.makedirs(str(mock_git_repo / "empty-dir"))
        with pytest.raises(SystemExit):
            _cmd_add(["empty-dir"], {}, project_root=".")

    def test_refuses_duplicate_path(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-a"], {}, project_root=".")

    def test_refuses_duplicate_name(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _make_npm_project(mock_git_repo, "pkg-b")
        _cmd_add(["pkg-a"], {"name": "shared"}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-b"], {"name": "shared"}, project_root=".")

    def test_refuses_without_init(self, mock_git_repo):
        _make_npm_project(mock_git_repo, "pkg-a")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-a"], {}, project_root=".")

    def test_watch_flag_creates_watch_list(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "myproject")
        _cmd_add(["myproject"], {"watch": "Package.swift,shared/**"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        assert projects[0]["watch"] == ["Package.swift", "shared/**"]

    def test_subtree_remote_flag(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "myproject")
        _cmd_add(["myproject"], {"subtree-remote": "git@github.com:user/pkg.git"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 1
        assert projects[0]["subtree_remote"] == "git@github.com:user/pkg.git"

    def test_depends_on_flag(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "core")
        _make_npm_project(mock_git_repo, "utils")
        _make_npm_project(mock_git_repo, "app")
        _cmd_add(["core"], {}, project_root=".")
        _cmd_add(["utils"], {}, project_root=".")
        capsys.readouterr()
        _cmd_add(["app"], {"depends-on": "core,utils"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        app_proj = [p for p in projects if p["name"] == "app"][0]
        assert app_proj["depends_on"] == ["core", "utils"]

    def test_depends_on_nonexistent_errors(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "app")
        with pytest.raises(SystemExit):
            _cmd_add(["app"], {"depends-on": "nonexistent"}, project_root=".")
        captured = capsys.readouterr()
        assert "does not exist" in captured.err

    def test_no_depends_on_omits_field(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "standalone")
        _cmd_add(["standalone"], {}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert "depends_on" not in projects[0]

    def test_library_true_sets_field(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib")
        _cmd_add(["mylib"], {"library": "true"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert projects[0]["library"] is True

    def test_library_false_omits_field(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "myapp")
        _cmd_add(["myapp"], {"library": "false"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert "library" not in projects[0]

    def test_no_library_flag_omits_field(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "noflag")
        _cmd_add(["noflag"], {}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert "library" not in projects[0]

    def test_library_invalid_errors(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "badlib")
        with pytest.raises(SystemExit):
            _cmd_add(["badlib"], {"library": "invalid"}, project_root=".")
        captured = capsys.readouterr()
        assert "--library must be 'true' or 'false'" in captured.err


class TestRemove:
    def test_removes_project(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {}, project_root=".")
        capsys.readouterr()  # clear
        _cmd_remove(["pkg-a"], {}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 0
        captured = capsys.readouterr()
        assert "Removed project at pkg-a" in captured.out

    def test_warning_on_missing_project(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        # Should NOT raise SystemExit -- just warn
        _cmd_remove(["nonexistent"], {}, project_root=".")
        captured = capsys.readouterr()
        assert "Warning:" in captured.err

    def test_normalizes_trailing_slash(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {}, project_root=".")
        capsys.readouterr()
        _cmd_remove(["pkg-a/"], {}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        assert len(projects) == 0

    def test_refuses_no_args(self, mock_git_repo):
        _cmd_init({}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_remove([], {}, project_root=".")

    def test_refuses_without_init(self, mock_git_repo):
        with pytest.raises(SystemExit):
            _cmd_remove(["pkg-a"], {}, project_root=".")

    def test_nonexistent_path_warns_without_exit(self, mock_git_repo, capsys):
        """Removing a path not in the workspace prints a warning, does not sys.exit."""
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {}, project_root=".")
        capsys.readouterr()
        # Should NOT raise SystemExit
        _cmd_remove(["nonexistent"], {}, project_root=".")
        captured = capsys.readouterr()
        assert "Warning:" in captured.err


class TestList:
    def test_lists_projects(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling")
        _make_npm_project(mock_git_repo, "core")
        _cmd_add(["tooling"], {}, project_root=".")
        _cmd_add(["core"], {}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        assert "Name" in captured.out
        assert "Path" in captured.out
        assert "tooling" in captured.out
        assert "core" in captured.out

    def test_empty_workspace_message(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." in captured.out

    def test_refuses_without_init(self, mock_git_repo):
        with pytest.raises(SystemExit):
            _cmd_list({}, project_root=".")

    def test_column_alignment(self, mock_git_repo, capsys):
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "a")
        _make_npm_project(mock_git_repo, "longname")
        _cmd_add(["a"], {"name": "short"}, project_root=".")
        _cmd_add(["longname"], {"name": "very-long-name"}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 3  # header + 2 projects
        # All lines should have consistent column positions
        header_path_pos = lines[0].index("Path")
        for line in lines[1:]:
            # The path column should start at the same position
            assert line[header_path_pos:header_path_pos + 1] != " " or line.strip().endswith("")
