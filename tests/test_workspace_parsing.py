"""Comprehensive tests for workspace.toml parsing: WorkspaceProject class and load/save functions.

Covers WorkspaceProject properties, dict-like access, equality, to_dict(),
load_workspace() validation and normalization, unknown field preservation,
save_workspace() round-trips, resolve_project(), and find_workspace_root().
"""

import os

import pytest

from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    WorkspaceProject,
    find_workspace_root,
    load_workspace,
    resolve_project,
    save_workspace,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)


# ---------------------------------------------------------------------------
# WorkspaceProject: typed properties with correct defaults
# ---------------------------------------------------------------------------


class TestWorkspaceProjectProperties:
    """WorkspaceProject exposes typed properties with sensible defaults."""

    def test_name(self):
        wp = WorkspaceProject({"name": "foo", "path": "packages/foo"})
        assert wp.name == "foo"

    def test_path(self):
        wp = WorkspaceProject({"name": "foo", "path": "packages/foo"})
        assert wp.path == "packages/foo"

    def test_watch_default_empty_list(self):
        wp = WorkspaceProject({"name": "x", "path": "x"})
        assert wp.watch == []

    def test_watch_explicit(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "watch": ["src/**", "*.toml"]})
        assert wp.watch == ["src/**", "*.toml"]

    def test_library_default_false(self):
        wp = WorkspaceProject({"name": "x", "path": "x"})
        assert wp.library is False

    def test_library_explicit_true(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "library": True})
        assert wp.library is True

    def test_library_coerces_truthy_int(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "library": 1})
        assert wp.library is True

    def test_dev_node_default_false(self):
        wp = WorkspaceProject({"name": "x", "path": "x"})
        assert wp.dev_node is False

    def test_dev_node_explicit_true(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "dev_node": True})
        assert wp.dev_node is True

    def test_depends_on_default_empty_list(self):
        wp = WorkspaceProject({"name": "x", "path": "x"})
        assert wp.depends_on == []

    def test_depends_on_explicit(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "depends_on": ["core", "util"]})
        assert wp.depends_on == ["core", "util"]

    def test_registry_name_default_empty_string(self):
        wp = WorkspaceProject({"name": "x", "path": "x"})
        assert wp.registry_name == ""

    def test_registry_name_explicit(self):
        wp = WorkspaceProject({"name": "x", "path": "x", "registry_name": "my-pkg"})
        assert wp.registry_name == "my-pkg"


# ---------------------------------------------------------------------------
# WorkspaceProject: dict-like access
# ---------------------------------------------------------------------------


class TestWorkspaceProjectDictAccess:
    """WorkspaceProject supports dict-like [], get(), in, and []= for backward compat."""

    def test_getitem_existing_key(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp["name"] == "foo"

    def test_getitem_missing_key_raises(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        with pytest.raises(KeyError):
            wp["nonexistent"]

    def test_setitem(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        wp["custom"] = 42
        assert wp["custom"] == 42

    def test_setitem_overwrites(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        wp["name"] = "bar"
        assert wp["name"] == "bar"
        assert wp.name == "bar"

    def test_contains_true(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert "name" in wp

    def test_contains_false(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert "missing" not in wp

    def test_get_existing(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp.get("name") == "foo"

    def test_get_missing_returns_none(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp.get("missing") is None

    def test_get_missing_with_default(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp.get("missing", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# WorkspaceProject: __eq__ comparison
# ---------------------------------------------------------------------------


class TestWorkspaceProjectEquality:
    """WorkspaceProject equality works against both WorkspaceProject and raw dict."""

    def test_equal_workspace_projects(self):
        a = WorkspaceProject({"name": "foo", "path": "p"})
        b = WorkspaceProject({"name": "foo", "path": "p"})
        assert a == b

    def test_unequal_workspace_projects(self):
        a = WorkspaceProject({"name": "foo", "path": "p"})
        b = WorkspaceProject({"name": "bar", "path": "q"})
        assert a != b

    def test_equal_to_raw_dict(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp == {"name": "foo", "path": "p"}

    def test_raw_dict_equal_to_workspace_project(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        # dict.__eq__ doesn't know about WorkspaceProject, but Python falls
        # back to the reflected comparison, so this should work.
        assert {"name": "foo", "path": "p"} == wp

    def test_unequal_to_different_dict(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp != {"name": "bar", "path": "q"}

    def test_not_equal_to_string(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp != "not a dict"

    def test_not_equal_to_none(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp != None  # noqa: E711

    def test_not_equal_to_int(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp != 42

    def test_equality_includes_extra_fields(self):
        a = WorkspaceProject({"name": "foo", "path": "p", "library": True})
        b = WorkspaceProject({"name": "foo", "path": "p"})
        assert a != b


# ---------------------------------------------------------------------------
# WorkspaceProject: to_dict() round-trip
# ---------------------------------------------------------------------------


class TestWorkspaceProjectToDict:
    """to_dict() returns the underlying data and supports round-trip construction."""

    def test_to_dict_returns_data(self):
        data = {"name": "foo", "path": "p", "library": True}
        wp = WorkspaceProject(data)
        assert wp.to_dict() is data

    def test_to_dict_roundtrip(self):
        data = {"name": "foo", "path": "p", "watch": ["*.py"], "custom": 99}
        wp = WorkspaceProject(data)
        reconstructed = WorkspaceProject(wp.to_dict())
        assert reconstructed == wp

    def test_mutations_via_dict_visible_through_properties(self):
        data = {"name": "foo", "path": "p"}
        wp = WorkspaceProject(data)
        wp.to_dict()["library"] = True
        assert wp.library is True


# ---------------------------------------------------------------------------
# WorkspaceProject: __repr__
# ---------------------------------------------------------------------------


class TestWorkspaceProjectRepr:
    """__repr__ produces a useful debug string."""

    def test_repr_contains_class_name(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        r = repr(wp)
        assert r.startswith("WorkspaceProject(")
        assert r.endswith(")")

    def test_repr_contains_data(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        r = repr(wp)
        assert "'name': 'foo'" in r
        assert "'path': 'p'" in r


# ---------------------------------------------------------------------------
# load_workspace(): validation errors
# ---------------------------------------------------------------------------


class TestLoadWorkspaceValidation:
    """load_workspace() raises clear errors on malformed workspace.toml."""

    def test_path_not_a_string_raises(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            "[[projects]]\npath = 123\nname = \"foo\"\n"
        )
        with pytest.raises(WorkspaceError, match="missing required 'path' string"):
            load_workspace(str(tmp_project))

    def test_path_is_bool_raises(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            "[[projects]]\npath = true\nname = \"foo\"\n"
        )
        with pytest.raises(WorkspaceError, match="missing required 'path' string"):
            load_workspace(str(tmp_project))

    def test_entry_not_a_table_raises(self, tmp_project):
        """If the TOML has projects as an array of non-tables, it should error.

        In practice TOML's [[projects]] syntax always produces tables, but
        programmatic callers could pass something weird.
        """
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        # A TOML array of strings instead of tables
        (ws_dir / WORKSPACE_FILE).write_text('projects = ["a", "b"]\n')
        with pytest.raises(WorkspaceError):
            load_workspace(str(tmp_project))

    def test_empty_projects_list_ok(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text("projects = []\n")
        result = load_workspace(str(tmp_project))
        assert result == []


# ---------------------------------------------------------------------------
# load_workspace(): normalization
# ---------------------------------------------------------------------------


class TestLoadWorkspaceNormalization:
    """load_workspace() normalizes paths and infers names."""

    def test_multiple_trailing_slashes_stripped(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "packages/foo///"\nname = "foo"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0].path == "packages/foo"

    def test_name_defaults_to_last_component(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "deeply/nested/my-lib"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0].name == "my-lib"

    def test_empty_name_gets_overridden(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "packages/foo"\nname = ""\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0].name == "foo"

    def test_returns_workspace_project_instances(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "pkg"\nname = "pkg"\n'
        )
        result = load_workspace(str(tmp_project))
        assert isinstance(result[0], WorkspaceProject)


# ---------------------------------------------------------------------------
# load_workspace(): unknown field preservation
# ---------------------------------------------------------------------------


class TestLoadWorkspaceUnknownFields:
    """Extra/unknown fields in project entries survive load_workspace()."""

    def test_unknown_scalar_field_preserved(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "p"\nname = "p"\ncustom_field = "hello"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["custom_field"] == "hello"

    def test_unknown_list_field_preserved(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "p"\nname = "p"\ntags = ["a", "b"]\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["tags"] == ["a", "b"]

    def test_unknown_bool_field_preserved(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "p"\nname = "p"\nexperimental = true\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0]["experimental"] is True

    def test_unknown_fields_accessible_via_get(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "p"\nname = "p"\nowner = "alice"\n'
        )
        result = load_workspace(str(tmp_project))
        assert result[0].get("owner") == "alice"


# ---------------------------------------------------------------------------
# save_workspace() round-trip: preserves all data including unknown fields
# ---------------------------------------------------------------------------


class TestSaveWorkspaceRoundTrip:
    """save_workspace() -> load_workspace() preserves all project data."""

    def test_full_roundtrip_with_all_known_fields(self, tmp_project):
        data = {
            "path": "libs/core",
            "name": "core",
            "watch": ["src/**"],
            "library": True,
            "dev_node": False,
            "depends_on": ["util"],
            "registry_name": "my-core",
        }
        wp = WorkspaceProject(data)
        save_workspace(str(tmp_project), [wp])
        loaded = load_workspace(str(tmp_project))
        assert len(loaded) == 1
        assert loaded[0].name == "core"
        assert loaded[0].path == "libs/core"
        assert loaded[0].watch == ["src/**"]
        assert loaded[0].library is True
        assert loaded[0].dev_node is False
        assert loaded[0].depends_on == ["util"]
        assert loaded[0].registry_name == "my-core"

    def test_roundtrip_preserves_unknown_fields(self, tmp_project):
        data = {
            "path": "pkg",
            "name": "pkg",
            "custom_string": "hello",
            "custom_int": 42,
            "custom_list": [1, 2, 3],
        }
        wp = WorkspaceProject(data)
        save_workspace(str(tmp_project), [wp])
        loaded = load_workspace(str(tmp_project))
        assert loaded[0]["custom_string"] == "hello"
        assert loaded[0]["custom_int"] == 42
        assert loaded[0]["custom_list"] == [1, 2, 3]

    def test_roundtrip_multiple_projects(self, tmp_project):
        projects = [
            WorkspaceProject({"path": "a", "name": "alpha", "library": True}),
            WorkspaceProject({"path": "b/c", "name": "charlie", "dev_node": True}),
            WorkspaceProject({"path": "d", "name": "delta", "depends_on": ["alpha"]}),
        ]
        save_workspace(str(tmp_project), projects)
        loaded = load_workspace(str(tmp_project))
        assert len(loaded) == 3
        assert loaded[0].name == "alpha"
        assert loaded[0].library is True
        assert loaded[1].name == "charlie"
        assert loaded[1].dev_node is True
        assert loaded[2].name == "delta"
        assert loaded[2].depends_on == ["alpha"]

    def test_save_accepts_both_dicts_and_workspace_projects(self, tmp_project):
        mixed = [
            WorkspaceProject({"path": "a", "name": "a"}),
            {"path": "b", "name": "b"},
        ]
        save_workspace(str(tmp_project), mixed)
        loaded = load_workspace(str(tmp_project))
        assert len(loaded) == 2
        assert loaded[0].name == "a"
        assert loaded[1].name == "b"

    def test_empty_projects_roundtrip(self, tmp_project):
        save_workspace(str(tmp_project), [])
        loaded = load_workspace(str(tmp_project))
        assert loaded == []


# ---------------------------------------------------------------------------
# resolve_project(): basic resolution and edge cases
# ---------------------------------------------------------------------------


class TestResolveProjectExtended:
    """Extended tests for resolve_project() beyond what test_workspace.py covers."""

    def test_exact_project_dir_match(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "pkg"\nname = "pkg"\n'
        )
        pkg = tmp_project / "pkg"
        pkg.mkdir()
        result = resolve_project(str(tmp_project), str(pkg))
        assert result is not None
        assert result.name == "pkg"

    def test_nested_project_wins_over_parent(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "mono"\nname = "mono"\n\n'
            '[[projects]]\npath = "mono/sub"\nname = "sub"\n'
        )
        (tmp_project / "mono" / "sub").mkdir(parents=True)
        result = resolve_project(str(tmp_project), str(tmp_project / "mono" / "sub"))
        assert result.name == "sub"

    def test_parent_project_for_sibling_of_nested(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "mono"\nname = "mono"\n\n'
            '[[projects]]\npath = "mono/sub"\nname = "sub"\n'
        )
        (tmp_project / "mono" / "other").mkdir(parents=True)
        result = resolve_project(str(tmp_project), str(tmp_project / "mono" / "other"))
        assert result.name == "mono"

    def test_no_match_returns_none(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "pkg"\nname = "pkg"\n'
        )
        (tmp_project / "unrelated").mkdir()
        result = resolve_project(str(tmp_project), str(tmp_project / "unrelated"))
        assert result is None

    def test_resolve_returns_workspace_project_instance(self, tmp_project):
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[projects]]\npath = "pkg"\nname = "pkg"\nlibrary = true\n'
        )
        pkg = tmp_project / "pkg"
        pkg.mkdir()
        result = resolve_project(str(tmp_project), str(pkg))
        assert isinstance(result, WorkspaceProject)
        assert result.library is True


# ---------------------------------------------------------------------------
# find_workspace_root(): directory traversal
# ---------------------------------------------------------------------------


class TestFindWorkspaceRootExtended:
    """Extended tests for find_workspace_root() directory walking."""

    def test_stops_at_filesystem_root(self, tmp_project):
        # A path deep within tmp_project with no workspace.toml anywhere
        deep = tmp_project / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        assert find_workspace_root(str(deep)) is None

    def test_finds_nearest_workspace_root(self, tmp_project):
        # Workspace at root
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text("projects = []\n")
        # Check from a child directory
        child = tmp_project / "some" / "deep" / "dir"
        child.mkdir(parents=True)
        result = find_workspace_root(str(child))
        assert result == str(tmp_project)

    def test_workspace_dir_without_file_not_found(self, tmp_project):
        # Only the directory exists, not the file
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        assert find_workspace_root(str(tmp_project)) is None

    def test_resolves_symlinks(self, tmp_project):
        # Create real workspace
        ws_dir = tmp_project / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / WORKSPACE_FILE).write_text("projects = []\n")
        # Create a symlink to a child dir
        real_dir = tmp_project / "real"
        real_dir.mkdir()
        link = tmp_project / "link"
        link.symlink_to(real_dir)
        result = find_workspace_root(str(link))
        assert result == str(tmp_project)
