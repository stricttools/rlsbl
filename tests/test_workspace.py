"""Tests for rlsbl.workspace."""

import os

import pytest

from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    find_workspace_root,
    load_workspace,
    resolve_project,
    save_workspace,
)


class TestFindWorkspaceRoot:
    """Tests for find_workspace_root."""

    def test_finds_workspace_in_current_dir(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[projects]\n')
        assert find_workspace_root(str(tmp_project)) == str(tmp_project)

    def test_finds_workspace_in_parent(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[projects]\n')
        child = tmp_project / "packages" / "foo"
        child.mkdir(parents=True)
        assert find_workspace_root(str(child)) == str(tmp_project)

    def test_returns_none_when_missing(self, tmp_project):
        assert find_workspace_root(str(tmp_project)) is None

    def test_uses_cwd_by_default(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[projects]\n')
        assert find_workspace_root() == str(tmp_project)

    def test_returns_none_for_deep_path_without_workspace(self, tmp_project):
        deep = tmp_project / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        assert find_workspace_root(str(deep)) is None


class TestLoadWorkspace:
    """Tests for load_workspace."""

    def test_loads_valid_toml(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/foo"\nname = "foo"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result == [{"path": "packages/foo", "name": "foo"}]

    def test_raises_file_not_found(self, tmp_project):
        with pytest.raises(FileNotFoundError):
            load_workspace(str(tmp_project))

    def test_raises_workspace_error_missing_projects_key(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('title = "hello"\n')
        with pytest.raises(WorkspaceError, match="missing required 'projects' key"):
            load_workspace(str(tmp_project))

    def test_raises_workspace_error_projects_not_list(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[projects]\nfoo = "bar"\n')
        with pytest.raises(WorkspaceError, match="must be a list"):
            load_workspace(str(tmp_project))

    def test_raises_workspace_error_missing_path(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text('[[projects]]\nname = "foo"\n')
        with pytest.raises(WorkspaceError, match="missing required 'path'"):
            load_workspace(str(tmp_project))

    def test_name_defaults_to_basename(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/my-lib"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result == [{"path": "packages/my-lib", "name": "my-lib"}]

    def test_multiple_projects(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "a"\nname = "alpha"\n\n'
            '[[projects]]\npath = "b"\nname = "beta"\n'
        )
        result = load_workspace(str(tmp_project))
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"


class TestSaveWorkspace:
    """Tests for save_workspace."""

    def test_creates_file(self, tmp_project):
        projects = [{"path": "packages/foo", "name": "foo"}]
        save_workspace(str(tmp_project), projects)
        assert (tmp_project / ".rlsbl-monorepo" / "workspace.toml").exists()

    def test_creates_directory(self, tmp_project):
        assert not (tmp_project / ".rlsbl-monorepo").exists()
        save_workspace(str(tmp_project), [{"path": "x", "name": "x"}])
        assert (tmp_project / ".rlsbl-monorepo").is_dir()

    def test_roundtrips_with_load(self, tmp_project):
        projects = [
            {"path": "packages/foo", "name": "foo"},
            {"path": "libs/bar", "name": "bar"},
        ]
        save_workspace(str(tmp_project), projects)
        loaded = load_workspace(str(tmp_project))
        assert loaded == projects

    def test_atomic_write_no_leftover_tmp(self, tmp_project):
        save_workspace(str(tmp_project), [{"path": "x", "name": "x"}])
        ws_dir = tmp_project / ".rlsbl-monorepo"
        files = list(ws_dir.iterdir())
        assert all(not f.name.endswith(".tmp") for f in files)

    def test_overwrites_existing(self, tmp_project):
        save_workspace(str(tmp_project), [{"path": "a", "name": "a"}])
        save_workspace(str(tmp_project), [{"path": "b", "name": "b"}])
        loaded = load_workspace(str(tmp_project))
        assert loaded == [{"path": "b", "name": "b"}]


class TestLoadWorkspacePathNormalization:
    """load_workspace must strip trailing slashes from project paths."""

    def test_strips_trailing_slash(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "auth-gateway/"\nname = "auth-gateway"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["path"] == "auth-gateway"

    def test_passes_through_when_no_trailing_slash(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "auth-gateway"\nname = "auth-gateway"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["path"] == "auth-gateway"

    def test_strips_trailing_slash_on_nested_path(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/foo/"\nname = "foo"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["path"] == "packages/foo"


class TestWorkspaceExtraKeys:
    """Tests for extra-key preservation in save_workspace round-trips."""

    def test_extra_keys_survive_roundtrip(self, tmp_project):
        projects = [
            {
                "path": "packages/foo",
                "name": "foo",
                "watch": ["Package.swift"],
            }
        ]
        save_workspace(str(tmp_project), projects)
        loaded = load_workspace(str(tmp_project))
        assert loaded == projects
        assert loaded[0]["watch"] == ["Package.swift"]

    def test_save_preserves_key_order(self, tmp_project):
        projects = [
            {
                "path": "libs/bar",
                "name": "bar",
                "watch": ["src/**"],
                "subtree_remote": "git@example.com:bar.git",
            }
        ]
        save_workspace(str(tmp_project), projects)
        toml_path = tmp_project / ".rlsbl-monorepo" / "workspace.toml"
        content = toml_path.read_text()
        # path must come before name, name before extras, extras sorted
        path_pos = content.index("path")
        name_pos = content.index("name")
        subtree_pos = content.index("subtree_remote")
        watch_pos = content.index("watch")
        assert path_pos < name_pos < subtree_pos < watch_pos


class TestResolveProject:
    """Tests for resolve_project."""

    def test_matches_project_by_path(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/foo"\nname = "foo"\n'
        )
        proj_dir = tmp_project / "packages" / "foo"
        proj_dir.mkdir(parents=True)
        result = resolve_project(str(tmp_project), str(proj_dir))
        assert result is not None
        assert result["name"] == "foo"

    def test_matches_subdirectory_of_project(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/foo"\nname = "foo"\n'
        )
        sub = tmp_project / "packages" / "foo" / "src" / "deep"
        sub.mkdir(parents=True)
        result = resolve_project(str(tmp_project), str(sub))
        assert result is not None
        assert result["name"] == "foo"

    def test_returns_none_outside_any_project(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages/foo"\nname = "foo"\n'
        )
        other = tmp_project / "other"
        other.mkdir()
        result = resolve_project(str(tmp_project), str(other))
        assert result is None

    def test_picks_most_specific_on_nested_paths(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "packages"\nname = "all"\n\n'
            '[[projects]]\npath = "packages/foo"\nname = "foo"\n'
        )
        inner = tmp_project / "packages" / "foo"
        inner.mkdir(parents=True)
        result = resolve_project(str(tmp_project), str(inner))
        assert result["name"] == "foo"

    def test_uses_cwd_by_default(self, tmp_project):
        ws_dir = tmp_project / ".rlsbl-monorepo"
        ws_dir.mkdir()
        proj_dir = tmp_project / "pkg"
        proj_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "pkg"\nname = "pkg"\n'
        )
        # cwd is tmp_project (set by tmp_project fixture), which is not inside "pkg"
        result = resolve_project(str(tmp_project))
        assert result is None
