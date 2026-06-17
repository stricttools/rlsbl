"""Tests for project root discovery (find_project_root, _require_sub_project_root, and main() integration)."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conftest import make_workspace, run_git
from rlsbl.utils import find_project_root
from rlsbl.workspace import WORKSPACE_DIR


class TestFindProjectRoot:
    """Unit tests for find_project_root()."""

    def test_find_root_in_current_dir(self, tmp_path, monkeypatch):
        """When .rlsbl/ is in cwd, returns cwd."""
        (tmp_path / ".rlsbl").mkdir()
        monkeypatch.chdir(tmp_path)
        assert find_project_root() == str(tmp_path)

    def test_find_root_in_parent(self, tmp_path, monkeypatch):
        """When .rlsbl/ is in parent, returns parent (subdirectory invocation)."""
        (tmp_path / ".rlsbl").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        assert find_project_root() == str(tmp_path)

    def test_find_root_deeply_nested(self, tmp_path, monkeypatch):
        """When .rlsbl/ is three levels up, returns correct ancestor."""
        (tmp_path / ".rlsbl").mkdir()
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert find_project_root() == str(tmp_path)

    def test_find_monorepo_root(self, tmp_path, monkeypatch):
        """When only .rlsbl-monorepo/ exists (no .rlsbl/), returns that dir."""
        (tmp_path / ".rlsbl-monorepo").mkdir()
        subdir = tmp_path / "packages" / "foo"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        assert find_project_root() == str(tmp_path)

    def test_prefers_rlsbl_over_monorepo(self, tmp_path, monkeypatch):
        """Sub-project has .rlsbl/, ancestor has .rlsbl-monorepo/: returns sub-project dir."""
        (tmp_path / ".rlsbl-monorepo").mkdir()
        subproject = tmp_path / "packages" / "foo"
        subproject.mkdir(parents=True)
        (subproject / ".rlsbl").mkdir()
        monkeypatch.chdir(subproject)
        assert find_project_root() == str(subproject)

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        """No markers anywhere, returns None."""
        # tmp_path has no .rlsbl/ or .rlsbl-monorepo/
        monkeypatch.chdir(tmp_path)
        assert find_project_root() is None

    def test_start_parameter(self, tmp_path):
        """Explicit start parameter overrides cwd."""
        (tmp_path / ".rlsbl").mkdir()
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        assert find_project_root(start=str(subdir)) == str(tmp_path)


class TestMainRootDiscovery:
    """Integration tests for root discovery in main()."""

    def test_main_no_chdir_for_project_commands(self, tmp_path, monkeypatch):
        """Project-dependent commands no longer chdir; CWD stays at subdirectory."""
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        subdir = tmp_path / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        original_cwd = os.getcwd()

        # Mock the status command's run_cmd to avoid actual execution
        with patch("rlsbl.commands.status.run_cmd") as mock_run:
            with patch("rlsbl.detect_registries", return_value=["npm"]):
                from rlsbl import app
                app.test(["status"])

        # CWD should remain unchanged (no os.chdir in _require_project_root)
        assert os.getcwd() == original_cwd

    def test_main_no_chdir_for_independent_commands(self, tmp_path, monkeypatch):
        """Independent commands (discover, check, watch) don't chdir."""
        (tmp_path / ".rlsbl").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        original_cwd = os.getcwd()

        with patch("rlsbl.commands.discover.run_cmd") as mock_run:
            from rlsbl import app
            app.test(["discover"])

        assert os.getcwd() == original_cwd

    def test_main_errors_when_no_root_found(self, tmp_path, monkeypatch):
        """Project commands fail with error when no .rlsbl/ found."""
        monkeypatch.chdir(tmp_path)

        from rlsbl import app
        result = app.test(["status"])
        assert result.exit_code == 1

    def test_scaffold_stays_in_cwd_for_new_project(self, tmp_path, monkeypatch):
        """Scaffold with no .rlsbl/ stays in cwd (new project init)."""
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        monkeypatch.chdir(tmp_path)
        original_cwd = os.getcwd()

        with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
            with patch("rlsbl.detect_registries", return_value=["npm"]):
                with patch("rlsbl.config.read_project_config", return_value={}):
                    from rlsbl import app
                    app.test(["scaffold"])

        assert os.getcwd() == original_cwd

    def test_scaffold_finds_root_for_update(self, tmp_path, monkeypatch):
        """Scaffold with .rlsbl/ in parent finds the root but does not chdir."""
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        subdir = tmp_path / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        original_cwd = os.getcwd()

        # First call: cwd check (src/ has no project files -> []).
        # Second call: auto-detect from root (has package.json -> ["npm"]).
        with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
            with patch("rlsbl.detect_registries", side_effect=[[], ["npm"]]):
                with patch("rlsbl.config.read_project_config", return_value={}):
                    from rlsbl import app
                    app.test(["scaffold"])

        # CWD should remain unchanged (no os.chdir in cmd_scaffold)
        assert os.getcwd() == original_cwd

    def test_scaffold_stays_in_subproject_with_project_files(self, tmp_path, monkeypatch):
        """Scaffold from a monorepo sub-project (with go.mod) stays in the sub-project."""
        # Monorepo root has .rlsbl-monorepo/
        (tmp_path / ".rlsbl-monorepo").mkdir()
        # Sub-project has go.mod (a project marker)
        subproject = tmp_path / "go"
        subproject.mkdir()
        (subproject / "go.mod").write_text("module example.com/test\n\ngo 1.21\n")
        (subproject / "VERSION").write_text("0.1.0\n")
        monkeypatch.chdir(subproject)
        original_cwd = os.getcwd()

        with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
            with patch("rlsbl.config.read_project_config", return_value={}):
                from rlsbl import app
                app.test(["scaffold"])

        # Should NOT have walked up to monorepo root
        assert os.getcwd() == original_cwd

    def test_scaffold_update_stays_in_subproject_with_rlsbl(self, tmp_path, monkeypatch):
        """Scaffold from a sub-project with .rlsbl/ stays in the sub-project."""
        # Monorepo root has .rlsbl-monorepo/
        (tmp_path / ".rlsbl-monorepo").mkdir()
        # Sub-project has .rlsbl/ and package.json
        subproject = tmp_path / "web"
        subproject.mkdir()
        (subproject / ".rlsbl").mkdir()
        (subproject / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
        monkeypatch.chdir(subproject)
        original_cwd = os.getcwd()

        with patch("rlsbl.commands.init_cmd.run_cmd") as mock_run:
            with patch("rlsbl.config.read_project_config", return_value={}):
                from rlsbl import app
                app.test(["scaffold"])

        # Should stay in sub-project, not walk to monorepo root
        assert os.getcwd() == original_cwd


class TestRequireSubProjectRoot:
    """Tests for _require_sub_project_root() resolution logic."""

    def _make_monorepo(self, tmp_path, monkeypatch, projects_spec):
        """Set up a monorepo git repo with workspace.toml and sub-projects.

        projects_spec: list of dicts with path, name, and optional has_rlsbl (bool).
        """
        monkeypatch.chdir(tmp_path)

        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")

        readme = tmp_path / "README.md"
        readme.write_text("# monorepo\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        ws_projects = [{"path": s["path"], "name": s["name"]} for s in projects_spec]
        make_workspace(tmp_path, ws_projects)

        for spec in projects_spec:
            proj_dir = tmp_path / spec["path"]
            proj_dir.mkdir(parents=True, exist_ok=True)
            # Every project gets a package.json so it's detectable
            (proj_dir / "package.json").write_text(
                json.dumps({"name": spec["name"], "version": "0.1.0"})
            )
            if spec.get("has_rlsbl"):
                (proj_dir / ".rlsbl").mkdir(exist_ok=True)

        run_git(tmp_path, "add", WORKSPACE_DIR)
        for spec in projects_spec:
            run_git(tmp_path, "add", spec["path"])
        run_git(tmp_path, "commit", "-q", "-m", "add projects")

        return tmp_path

    def test_sub_project_root_in_monorepo(self, tmp_path, monkeypatch):
        """Chdir into sub-project with .rlsbl/ -> returns sub-project path."""
        root = self._make_monorepo(tmp_path, monkeypatch, [
            {"path": "pkg-a", "name": "pkg-a", "has_rlsbl": True},
        ])
        sub = root / "pkg-a"
        monkeypatch.chdir(sub)

        from rlsbl import _require_sub_project_root
        result = _require_sub_project_root()

        assert result == sub

    def test_sub_project_root_without_rlsbl(self, tmp_path, monkeypatch):
        """Chdir into sub-project without .rlsbl/ -> returns sub-project path (not monorepo root).

        The workspace.toml lists the project, so resolve_project matches it
        even though it has no .rlsbl/ directory of its own.
        """
        root = self._make_monorepo(tmp_path, monkeypatch, [
            {"path": "pkg-b", "name": "pkg-b", "has_rlsbl": False},
        ])
        sub = root / "pkg-b"
        monkeypatch.chdir(sub)

        from rlsbl import _require_sub_project_root
        result = _require_sub_project_root()

        # Should resolve to the sub-project, not the monorepo root
        assert result == sub

    def test_sub_project_root_standalone(self, tmp_path, monkeypatch):
        """Standalone project (no monorepo) -> returns project root (same as _require_project_root)."""
        monkeypatch.chdir(tmp_path)

        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "standalone", "version": "1.0.0"})
        )
        run_git(tmp_path, "add", ".")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        from rlsbl import _require_sub_project_root
        result = _require_sub_project_root()

        assert result == tmp_path


class TestSymlinkPathNormalization:
    """Verify that path normalization resolves symlinks consistently.

    find_project_root and find_workspace_root both use os.path.realpath,
    so paths discovered through symlinks must agree with the real directory.
    """

    def test_find_project_root_resolves_symlink(self, tmp_path):
        """find_project_root called from a symlinked path returns the real path."""
        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        (real_dir / ".rlsbl").mkdir()

        link = tmp_path / "link_to_project"
        link.symlink_to(real_dir, target_is_directory=True)

        result = find_project_root(start=str(link))
        assert result == os.path.realpath(str(real_dir))

    def test_find_project_root_from_symlinked_subdirectory(self, tmp_path):
        """find_project_root from a subdirectory inside a symlinked tree resolves correctly."""
        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        (real_dir / ".rlsbl").mkdir()
        (real_dir / "src").mkdir()

        link = tmp_path / "link_to_project"
        link.symlink_to(real_dir, target_is_directory=True)

        result = find_project_root(start=str(link / "src"))
        assert result == os.path.realpath(str(real_dir))

    def test_find_workspace_root_resolves_symlink(self, tmp_path):
        """find_workspace_root called from a symlinked path returns the real path."""
        from rlsbl.workspace import find_workspace_root, WORKSPACE_DIR, WORKSPACE_FILE

        real_dir = tmp_path / "real_monorepo"
        real_dir.mkdir()
        ws_dir = real_dir / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text('[[projects]]\npath = "pkg"\nname = "pkg"\n')

        link = tmp_path / "link_to_monorepo"
        link.symlink_to(real_dir, target_is_directory=True)

        result = find_workspace_root(str(link))
        assert result == os.path.realpath(str(real_dir))

    def test_project_and_workspace_roots_agree_through_symlink(self, tmp_path):
        """Both root discovery functions agree when called through the same symlink."""
        from rlsbl.workspace import find_workspace_root, WORKSPACE_DIR, WORKSPACE_FILE

        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        (real_dir / ".rlsbl").mkdir()
        ws_dir = real_dir / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text('[[projects]]\npath = "."\nname = "root"\n')

        link = tmp_path / "link_to_project"
        link.symlink_to(real_dir, target_is_directory=True)

        project_root = find_project_root(start=str(link))
        workspace_root = find_workspace_root(str(link))

        expected = os.path.realpath(str(real_dir))
        assert project_root == expected
        assert workspace_root == expected
        assert project_root == workspace_root

    def test_containment_check_works_through_symlink(self, tmp_path):
        """startswith-based containment works when paths are resolved through symlinks."""
        from rlsbl.checks._common import _sibling_exclude_dirs

        real_dir = tmp_path / "real_root"
        real_dir.mkdir()
        (real_dir / "pkg-a").mkdir()
        (real_dir / "pkg-b").mkdir()

        link = tmp_path / "link_to_root"
        link.symlink_to(real_dir, target_is_directory=True)

        # _sibling_exclude_dirs uses os.path.realpath, so even if root is
        # provided as a symlink path, containment checks still work.
        all_projects = [
            {"path": ".", "name": "root"},
            {"path": "pkg-a", "name": "pkg-a"},
            {"path": "pkg-b", "name": "pkg-b"},
        ]
        excluded = _sibling_exclude_dirs(str(link), ".", all_projects)

        # Both pkg-a and pkg-b should be excluded (they are inside ".")
        real_root = os.path.realpath(str(real_dir))
        assert os.path.realpath(os.path.join(str(real_dir), "pkg-a")) in excluded
        assert os.path.realpath(os.path.join(str(real_dir), "pkg-b")) in excluded
        assert len(excluded) == 2
