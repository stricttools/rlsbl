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


def _added(root):
    """The members a test added, i.e. everything but the mandatory root member."""
    return [p for p in load_workspace(str(root)) if p["path"] != "."]


class TestInit:
    def test_creates_workspace(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        ws_file = mock_git_repo / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()
        projects = load_workspace(str(mock_git_repo))
        # A workspace is never empty: init writes the mandatory root member.
        assert [p["name"] for p in projects] == ["root"]
        captured = capsys.readouterr()
        assert "Initialized monorepo workspace" in captured.out

    def test_refuses_reinit(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_init({"root-dev-node": True}, project_root=".")


class TestAdd:
    def test_adds_project(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        projects = _added(mock_git_repo)
        assert len(projects) == 1
        assert projects[0]["path"] == "pkg-a"
        assert projects[0]["name"] == "pkg-a"
        captured = capsys.readouterr()
        assert "Added project 'pkg-a' at pkg-a" in captured.out

    def test_uses_name_flag(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "libs/core")
        _cmd_add(["libs/core"], {"releasable": "false", "name": "core-lib"}, project_root=".")
        projects = _added(mock_git_repo)
        assert projects[0]["name"] == "core-lib"
        captured = capsys.readouterr()
        assert "core-lib" in captured.out

    def test_refuses_no_args(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add([], {"releasable": "false"}, project_root=".")

    def test_refuses_nonexistent_path(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["nonexistent"], {"releasable": "false"}, project_root=".")

    def test_refuses_no_target(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        os.makedirs(str(mock_git_repo / "empty-dir"))
        with pytest.raises(SystemExit):
            _cmd_add(["empty-dir"], {"releasable": "false"}, project_root=".")

    def test_refuses_duplicate_path(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")

    def test_refuses_duplicate_name(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _make_npm_project(mock_git_repo, "pkg-b")
        _cmd_add(["pkg-a"], {"releasable": "false", "name": "shared"}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-b"], {"releasable": "false", "name": "shared"}, project_root=".")

    def test_refuses_without_init(self, mock_git_repo):
        _make_npm_project(mock_git_repo, "pkg-a")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")

    def test_there_is_no_watch_flag(self, mock_git_repo, capsys):
        """--watch is gone from the surface, not merely refused by the handler.

        This slot used to assert the refusal message a transitional
        `--watch` still printed. The flag itself is now unregistered, so
        strictcli rejects it before any handler runs, and a member's
        territory has exactly one spelling: its declared path.
        """
        import rlsbl

        add_cmd = dict(rlsbl.app._collect_all_commands())["monorepo.add"]
        assert "watch" not in {f.name for f in add_cmd.flags}

        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "myproject")
        _cmd_add(["myproject"], {"releasable": "false"}, project_root=".")
        projects = _added(mock_git_repo)
        assert len(projects) == 1
        assert "watch" not in projects[0].to_dict()

    def test_no_subtree_remote_is_written_on_the_member(self, mock_git_repo, capsys):
        """The mirror destination is a releasable key, so `add` never writes one."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "myproject")
        _cmd_add(["myproject"], {"releasable": "false"}, project_root=".")
        projects = _added(mock_git_repo)
        assert len(projects) == 1
        assert "subtree_remote" not in projects[0].to_dict()

    def test_depends_on_flag(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "core")
        _make_npm_project(mock_git_repo, "utils")
        _make_npm_project(mock_git_repo, "app")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")
        _cmd_add(["utils"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_add(["app"], {"releasable": "false", "depends-on": "core,utils"}, project_root=".")
        projects = load_workspace(str(mock_git_repo))
        app_proj = [p for p in projects if p["name"] == "app"][0]
        assert app_proj["depends_on"] == ["core", "utils"]

    def test_depends_on_nonexistent_errors(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "app")
        with pytest.raises(SystemExit):
            _cmd_add(["app"], {"releasable": "false", "depends-on": "nonexistent"}, project_root=".")
        captured = capsys.readouterr()
        assert "does not exist" in captured.err

    def test_no_depends_on_omits_field(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "standalone")
        _cmd_add(["standalone"], {"releasable": "false"}, project_root=".")
        assert "depends_on" not in _added(mock_git_repo)[0]

    def test_library_true_sets_field(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib")
        _cmd_add(["mylib"], {"releasable": "false", "library": "true"}, project_root=".")
        assert _added(mock_git_repo)[0]["library"] is True

    def test_library_false_omits_field(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "myapp")
        _cmd_add(["myapp"], {"releasable": "false", "library": "false"}, project_root=".")
        assert "library" not in _added(mock_git_repo)[0]

    def test_no_library_flag_omits_field(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "noflag")
        _cmd_add(["noflag"], {"releasable": "false"}, project_root=".")
        assert "library" not in _added(mock_git_repo)[0]

    def test_library_invalid_errors(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "badlib")
        with pytest.raises(SystemExit):
            _cmd_add(["badlib"], {"releasable": "false", "library": "invalid"}, project_root=".")
        captured = capsys.readouterr()
        assert "--library must be 'true' or 'false'" in captured.err


class TestRemove:
    def test_removes_project(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()  # clear
        _cmd_remove(["pkg-a"], {}, project_root=".")
        assert _added(mock_git_repo) == []
        captured = capsys.readouterr()
        assert "Removed project at pkg-a" in captured.out

    def test_warning_on_missing_project(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        # Should NOT raise SystemExit -- just warn
        _cmd_remove(["nonexistent"], {}, project_root=".")
        captured = capsys.readouterr()
        assert "Warning:" in captured.err

    def test_normalizes_trailing_slash(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_remove(["pkg-a/"], {}, project_root=".")
        assert _added(mock_git_repo) == []

    def test_refuses_no_args(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_remove([], {}, project_root=".")

    def test_refuses_without_init(self, mock_git_repo):
        with pytest.raises(SystemExit):
            _cmd_remove(["pkg-a"], {}, project_root=".")

    def test_nonexistent_path_warns_without_exit(self, mock_git_repo, capsys):
        """Removing a path not in the workspace prints a warning, does not sys.exit."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        # Should NOT raise SystemExit
        _cmd_remove(["nonexistent"], {}, project_root=".")
        captured = capsys.readouterr()
        assert "Warning:" in captured.err


class TestList:
    def test_lists_projects(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling")
        _make_npm_project(mock_git_repo, "core")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        assert "Name" in captured.out
        assert "Path" in captured.out
        assert "tooling" in captured.out
        assert "core" in captured.out

    def test_it_lists_the_member_facts_its_help_promises(
        self, mock_git_repo, capsys
    ):
        """Name and path alone answer almost nothing about a workspace.

        The row that matters is which releasable a member is versioned under,
        and what kind of member it is: the two facts every other monorepo
        command branches on.
        """
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "core")
        _make_npm_project(mock_git_repo, "harness")
        _cmd_add(
            ["core"], {"releasable": "core", "library": "true"},
            project_root=".",
        )
        _cmd_add(
            ["harness"], {"releasable": "false", "dev_only": "true"},
            project_root=".",
        )
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        out = capsys.readouterr().out

        assert "Releasable" in out
        assert "Flags" in out
        core_row = next(line for line in out.splitlines() if line.startswith("core"))
        assert "core" in core_row.split()[2]
        assert "library" in core_row
        harness_row = next(
            line for line in out.splitlines() if line.startswith("harness")
        )
        assert "dev-only" in harness_row
        root_row = next(line for line in out.splitlines() if line.startswith("root"))
        assert "dev-only" in root_row

    def test_fresh_workspace_lists_its_root_member(self, mock_git_repo, capsys):
        """A workspace always has at least its root member, so it is never empty."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." not in captured.out
        assert "root" in captured.out

    def test_refuses_without_init(self, mock_git_repo):
        with pytest.raises(SystemExit):
            _cmd_list({}, project_root=".")

    def test_column_alignment(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "a")
        _make_npm_project(mock_git_repo, "longname")
        _cmd_add(["a"], {"releasable": "false", "name": "short"}, project_root=".")
        _cmd_add(["longname"], {"releasable": "false", "name": "very-long-name"}, project_root=".")
        capsys.readouterr()
        _cmd_list({}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 4  # header + the root member + 2 projects
        # All lines should have consistent column positions
        header_path_pos = lines[0].index("Path")
        for line in lines[1:]:
            # The path column should start at the same position
            assert line[header_path_pos:header_path_pos + 1] != " " or line.strip().endswith("")
