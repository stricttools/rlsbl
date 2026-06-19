"""Comprehensive tests for the releasable data model.

Covers:
- Releasable dataclass construction and defaults
- WorkspaceProject.releasable property (string, false, None, invalid values)
- load_releasables() in explicit mode (happy path, validation errors)
- load_releasables() raises WorkspaceError when [[releasables]] is missing
- members_of() function
- save_workspace() round-trip with releasables
- Snapshot releasable section
"""

import json
import os

import pytest

from rlsbl.errors import WorkspaceError
from rlsbl.snapshot import generate_snapshot
from rlsbl.workspace import (
    DEFAULT_TAG_FORMAT,
    Releasable,
    WorkspaceProject,
    load_releasables,
    load_workspace,
    members_of,
    save_workspace,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)
from rlsbl.workspace_graph import WorkspaceGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path, content):
    """Write raw TOML content to workspace.toml in a temp directory."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(content)


def _make_workspace_with_targets(tmp_path, project_defs):
    """Create workspace dirs with project manifests for snapshot tests.

    project_defs is a list of dicts with: name, path, target, version,
    and optionally: description, library, test_only, depends_on, dev_node,
    releasable.
    """
    root = str(tmp_path)
    projects = []

    for pdef in project_defs:
        proj_dir = tmp_path / pdef["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)

        proj = {"path": pdef["path"], "name": pdef["name"]}
        for key in ("description", "library", "test_only", "depends_on",
                     "dev_node", "releasable"):
            if key in pdef:
                proj[key] = pdef[key]
        projects.append(proj)

        target = pdef.get("target", "pypi")
        version = pdef.get("version", "0.1.0")
        if target == "pypi":
            (proj_dir / "pyproject.toml").write_text(
                f'[project]\nname = "{pdef["name"]}"\nversion = "{version}"\n'
            )
        elif target == "npm":
            (proj_dir / "package.json").write_text(
                json.dumps({"name": pdef["name"], "version": version})
            )

    return root, projects


# ---------------------------------------------------------------------------
# Releasable dataclass: construction and defaults
# ---------------------------------------------------------------------------


class TestReleasableDataclass:
    """Releasable dataclass construction and field defaults."""

    def test_basic_construction(self):
        r = Releasable(name="core")
        assert r.name == "core"
        assert r.tag_format == DEFAULT_TAG_FORMAT

    def test_custom_tag_format(self):
        r = Releasable(name="www", tag_format="v{version}")
        assert r.name == "www"
        assert r.tag_format == "v{version}"

    def test_default_tag_format_value(self):
        assert DEFAULT_TAG_FORMAT == "{name}@v{version}"

    def test_empty_name_raises(self):
        with pytest.raises(WorkspaceError, match="non-empty string"):
            Releasable(name="")

    def test_equality(self):
        a = Releasable(name="core")
        b = Releasable(name="core")
        assert a == b

    def test_inequality_name(self):
        a = Releasable(name="core")
        b = Releasable(name="www")
        assert a != b

    def test_inequality_tag_format(self):
        a = Releasable(name="core", tag_format="v{version}")
        b = Releasable(name="core", tag_format="{name}@v{version}")
        assert a != b

    def test_repr_contains_name(self):
        r = Releasable(name="myrel")
        assert "myrel" in repr(r)


# ---------------------------------------------------------------------------
# WorkspaceProject.releasable property
# ---------------------------------------------------------------------------


class TestWorkspaceProjectReleasableProperty:
    """WorkspaceProject.releasable returns str, False, or None."""

    def test_not_set_returns_none(self):
        wp = WorkspaceProject({"name": "foo", "path": "p"})
        assert wp.releasable is None

    def test_string_value(self):
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": "core"})
        assert wp.releasable == "core"

    def test_false_value(self):
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": False})
        assert wp.releasable is False

    def test_true_value_raises(self):
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": True})
        with pytest.raises(WorkspaceError, match="releasable = true is not valid"):
            wp.releasable

    def test_integer_value_raises(self):
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": 42})
        with pytest.raises(WorkspaceError, match="must be a string or false"):
            wp.releasable

    def test_list_value_raises(self):
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": ["a"]})
        with pytest.raises(WorkspaceError, match="must be a string or false"):
            wp.releasable

    def test_empty_string_value(self):
        """Empty string is technically valid at the property level; validation
        catches it in load_releasables when it doesn't match any defined name."""
        wp = WorkspaceProject({"name": "foo", "path": "p", "releasable": ""})
        assert wp.releasable == ""


# ---------------------------------------------------------------------------
# load_releasables: explicit mode -- happy path
# ---------------------------------------------------------------------------


class TestLoadReleasablesExplicitHappy:
    """load_releasables() with [[releasables]] section: happy paths."""

    def test_single_releasable_single_project(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "pkg"
name = "pkg"
releasable = "core"
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"
        assert releasables[0].tag_format == DEFAULT_TAG_FORMAT

    def test_multiple_releasables(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[releasables]]
name = "www"
tag_format = "v{version}"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "b"
name = "b"
releasable = "www"
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 2
        names = {r.name for r in releasables}
        assert names == {"core", "www"}
        www = [r for r in releasables if r.name == "www"][0]
        assert www.tag_format == "v{version}"

    def test_project_with_false_releasable(self, tmp_project):
        """releasable = false is accepted and doesn't need to match a releasable name."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "b"
name = "b"
releasable = false
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"

    def test_dev_node_project_skipped_in_validation(self, tmp_project):
        """dev_node projects don't need a releasable field in explicit mode."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "tests"
name = "tests"
dev_node = true
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1

    def test_multiple_projects_same_releasable(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"

[[projects]]
path = "b"
name = "b"
releasable = "core"

[[projects]]
path = "c"
name = "c"
releasable = "core"
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"

    def test_accepts_preloaded_projects(self, tmp_project):
        """Passing projects= avoids a second load_workspace call."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        projects = load_workspace(str(tmp_project))
        releasables = load_releasables(str(tmp_project), projects=projects)
        assert len(releasables) == 1


# ---------------------------------------------------------------------------
# load_releasables: explicit mode -- validation errors
# ---------------------------------------------------------------------------


class TestLoadReleasablesExplicitErrors:
    """load_releasables() with [[releasables]] section: error cases."""

    def test_missing_releasable_field(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
""")
        with pytest.raises(WorkspaceError, match="missing required 'releasable' field"):
            load_releasables(str(tmp_project))

    def test_invalid_releasable_reference(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "nonexistent"
""")
        with pytest.raises(WorkspaceError, match="does not match any defined releasable"):
            load_releasables(str(tmp_project))

    def test_releasable_true_rejected(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = true
""")
        with pytest.raises(WorkspaceError, match="releasable = true is not valid"):
            load_releasables(str(tmp_project))

    def test_duplicate_releasable_name(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        with pytest.raises(WorkspaceError, match="duplicate releasable name"):
            load_releasables(str(tmp_project))

    def test_releasable_missing_name(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
tag_format = "v{version}"

[[projects]]
path = "a"
name = "a"
releasable = false
""")
        with pytest.raises(WorkspaceError, match="missing required 'name' string"):
            load_releasables(str(tmp_project))

    def test_releasable_name_not_string(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = 42

[[projects]]
path = "a"
name = "a"
releasable = false
""")
        with pytest.raises(WorkspaceError, match="missing required 'name' string"):
            load_releasables(str(tmp_project))

    def test_releasable_tag_format_not_string(self, tmp_project):
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"
tag_format = 42

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        with pytest.raises(WorkspaceError, match="tag_format must be a string"):
            load_releasables(str(tmp_project))

    def test_releasables_not_list(self, tmp_project):
        """releasables = "core" instead of [[releasables]]."""
        _write_workspace(tmp_project, """\
releasables = "core"

[[projects]]
path = "a"
name = "a"
""")
        with pytest.raises(WorkspaceError, match="must be a list of tables"):
            load_releasables(str(tmp_project))

    def test_releasable_entry_not_a_table(self, tmp_project):
        """releasables = ["core"] instead of [[releasables]] with tables."""
        _write_workspace(tmp_project, """\
releasables = ["core"]

[[projects]]
path = "a"
name = "a"
""")
        with pytest.raises(WorkspaceError, match="must be a table"):
            load_releasables(str(tmp_project))


# ---------------------------------------------------------------------------
# load_releasables: missing [[releasables]] section
# ---------------------------------------------------------------------------


class TestLoadReleasablesMissingSection:
    """load_releasables() raises WorkspaceError when [[releasables]] is absent."""

    def test_missing_releasables_section_raises(self, tmp_project):
        _write_workspace(tmp_project, """\
[[projects]]
path = "a"
name = "alpha"
""")
        with pytest.raises(WorkspaceError, match=r"\[\[releasables\]\] section required"):
            load_releasables(str(tmp_project))

    def test_empty_workspace_raises(self, tmp_project):
        _write_workspace(tmp_project, "projects = []\n")
        with pytest.raises(WorkspaceError, match=r"\[\[releasables\]\] section required"):
            load_releasables(str(tmp_project))


# ---------------------------------------------------------------------------
# members_of function
# ---------------------------------------------------------------------------


class TestMembersOf:
    """members_of() returns correct project subsets."""

    def test_explicit_single_member(self):
        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "core"}),
            WorkspaceProject({"name": "b", "path": "b", "releasable": "www"}),
        ]
        result = members_of("core", projects)
        assert len(result) == 1
        assert result[0].name == "a"

    def test_explicit_multiple_members(self):
        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "core"}),
            WorkspaceProject({"name": "b", "path": "b", "releasable": "core"}),
            WorkspaceProject({"name": "c", "path": "c", "releasable": "www"}),
        ]
        result = members_of("core", projects)
        assert len(result) == 2
        names = {p.name for p in result}
        assert names == {"a", "b"}

    def test_explicit_false_excluded(self):
        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "core"}),
            WorkspaceProject({"name": "b", "path": "b", "releasable": False}),
        ]
        result = members_of("core", projects)
        assert len(result) == 1
        assert result[0].name == "a"

    def test_no_releasable_field_not_matched(self):
        """Project without releasable field is not matched by members_of."""
        projects = [
            WorkspaceProject({"name": "alpha", "path": "a"}),
            WorkspaceProject({"name": "beta", "path": "b"}),
        ]
        result = members_of("alpha", projects)
        assert result == []

    def test_no_members(self):
        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "core"}),
        ]
        result = members_of("nonexistent", projects)
        assert result == []

    def test_empty_projects(self):
        result = members_of("anything", [])
        assert result == []

    def test_only_explicit_field_matched(self):
        """Only projects with explicit releasable field are matched."""
        projects = [
            WorkspaceProject({"name": "core", "path": "core", "releasable": "www"}),
            WorkspaceProject({"name": "other", "path": "other"}),
        ]
        # "core" releasable has no members -- the "core" project belongs to "www"
        assert members_of("core", projects) == []
        # "other" has no releasable field -- not a member of anything
        assert members_of("other", projects) == []
        result = members_of("www", projects)
        assert len(result) == 1
        assert result[0].name == "core"


# ---------------------------------------------------------------------------
# save_workspace round-trip with releasables
# ---------------------------------------------------------------------------


class TestSaveWorkspaceReleasablesRoundTrip:
    """save_workspace() preserves [[releasables]] and releasable fields."""

    def test_releasable_field_on_projects_roundtrip(self, tmp_project):
        """Project releasable fields survive save -> load."""
        projects = [
            WorkspaceProject({"path": "a", "name": "a", "releasable": "core"}),
            WorkspaceProject({"path": "b", "name": "b", "releasable": False}),
            WorkspaceProject({"path": "c", "name": "c"}),
        ]
        save_workspace(str(tmp_project), projects)
        loaded = load_workspace(str(tmp_project))
        assert loaded[0].releasable == "core"
        assert loaded[1].releasable is False
        assert loaded[2].releasable is None

    def test_releasables_section_preserved_when_not_passed(self, tmp_project):
        """When releasables=None, existing [[releasables]] is preserved."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"
tag_format = "v{version}"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        projects = load_workspace(str(tmp_project))
        # Save without touching releasables
        save_workspace(str(tmp_project), projects)
        # Re-load and verify releasables section survived
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"
        assert releasables[0].tag_format == "v{version}"

    def test_releasables_section_written_when_passed(self, tmp_project):
        """When releasables are passed, they are written."""
        projects = [
            WorkspaceProject({"path": "a", "name": "a", "releasable": "core"}),
        ]
        rels = [Releasable(name="core", tag_format="v{version}")]
        save_workspace(str(tmp_project), projects, releasables=rels)

        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"
        assert releasables[0].tag_format == "v{version}"

    def test_releasables_with_default_tag_format_omitted(self, tmp_project):
        """Default tag_format is not written to TOML (keeps it clean)."""
        projects = [
            WorkspaceProject({"path": "a", "name": "a", "releasable": "core"}),
        ]
        rels = [Releasable(name="core")]
        save_workspace(str(tmp_project), projects, releasables=rels)

        # Read raw TOML to verify tag_format not written
        ws_file = tmp_project / WORKSPACE_DIR / WORKSPACE_FILE
        content = ws_file.read_text()
        assert "tag_format" not in content

        # But loading still gives the default
        releasables = load_releasables(str(tmp_project))
        assert releasables[0].tag_format == DEFAULT_TAG_FORMAT

    def test_empty_releasables_list_removes_section(self, tmp_project):
        """Passing releasables=[] removes the [[releasables]] section."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = "core"
""")
        projects = load_workspace(str(tmp_project))
        save_workspace(str(tmp_project), projects, releasables=[])

        # Without [[releasables]], load_releasables raises
        with pytest.raises(WorkspaceError, match=r"\[\[releasables\]\] section required"):
            load_releasables(str(tmp_project))

    def test_full_roundtrip_explicit_mode(self, tmp_project):
        """Full round-trip: write releasables + projects, then load both."""
        rels = [
            Releasable(name="core"),
            Releasable(name="www", tag_format="v{version}"),
        ]
        projects = [
            WorkspaceProject({"path": "a", "name": "a", "releasable": "core"}),
            WorkspaceProject({"path": "b", "name": "b", "releasable": "core"}),
            WorkspaceProject({"path": "c", "name": "c", "releasable": "www"}),
            WorkspaceProject({"path": "d", "name": "d", "releasable": False}),
            WorkspaceProject({"path": "tests", "name": "tests", "dev_node": True}),
        ]
        save_workspace(str(tmp_project), projects, releasables=rels)

        loaded_projects = load_workspace(str(tmp_project))
        loaded_releasables = load_releasables(str(tmp_project), projects=loaded_projects)

        assert len(loaded_releasables) == 2
        core = [r for r in loaded_releasables if r.name == "core"][0]
        www = [r for r in loaded_releasables if r.name == "www"][0]
        assert core.tag_format == DEFAULT_TAG_FORMAT
        assert www.tag_format == "v{version}"

        assert loaded_projects[0].releasable == "core"
        assert loaded_projects[1].releasable == "core"
        assert loaded_projects[2].releasable == "www"
        assert loaded_projects[3].releasable is False
        assert loaded_projects[4].dev_node is True

    def test_multiple_save_load_cycles(self, tmp_project):
        """Repeated save/load cycles don't corrupt data."""
        rels = [Releasable(name="core")]
        projects = [
            WorkspaceProject({"path": "a", "name": "a", "releasable": "core"}),
        ]
        for _ in range(3):
            save_workspace(str(tmp_project), projects, releasables=rels)
            projects = load_workspace(str(tmp_project))
            loaded_rels = load_releasables(str(tmp_project), projects=projects)
            assert len(loaded_rels) == 1
            assert loaded_rels[0].name == "core"
            assert projects[0].releasable == "core"


# ---------------------------------------------------------------------------
# Snapshot: releasable section
# ---------------------------------------------------------------------------


class TestSnapshotReleasables:
    """generate_snapshot() includes releasable information when provided."""

    def test_no_releasables_backward_compat(self, tmp_path):
        """Without releasables, snapshot has no releasables section."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)
        assert "releasables" not in snapshot
        assert "releasable" not in snapshot["packages"]["alpha"]

    def test_explicit_releasables_single_member(self, tmp_path):
        """With explicit releasables, each project maps to its releasable."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0", "releasable": "alpha"},
            {"name": "beta", "path": "packages/beta", "target": "pypi",
             "version": "0.2.0", "releasable": "beta"},
        ])
        graph = WorkspaceGraph(root, projects)
        releasables = [Releasable(name="alpha"), Releasable(name="beta")]
        snapshot = generate_snapshot(root, projects, graph, releasables=releasables)

        assert "releasables" in snapshot
        assert set(snapshot["releasables"].keys()) == {"alpha", "beta"}
        assert snapshot["releasables"]["alpha"]["members"] == ["alpha"]
        assert snapshot["releasables"]["alpha"]["version"] is None
        assert snapshot["releasables"]["alpha"]["tag_format"] == DEFAULT_TAG_FORMAT

    def test_explicit_releasables(self, tmp_path):
        """With explicit releasables, members are derived correctly."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "a", "path": "packages/a", "target": "pypi",
             "version": "1.0.0", "releasable": "core"},
            {"name": "b", "path": "packages/b", "target": "pypi",
             "version": "1.0.0", "releasable": "core"},
            {"name": "c", "path": "packages/c", "target": "pypi",
             "version": "0.5.0", "releasable": "www"},
        ])
        graph = WorkspaceGraph(root, projects)
        releasables = [
            Releasable(name="core"),
            Releasable(name="www", tag_format="v{version}"),
        ]
        snapshot = generate_snapshot(root, projects, graph, releasables=releasables)

        assert snapshot["releasables"]["core"]["members"] == ["a", "b"]
        assert snapshot["releasables"]["www"]["members"] == ["c"]
        assert snapshot["releasables"]["www"]["tag_format"] == "v{version}"

    def test_per_package_releasable_field(self, tmp_path):
        """Each package entry gets a releasable field."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "a", "path": "packages/a", "target": "pypi",
             "version": "1.0.0", "releasable": "core"},
            {"name": "b", "path": "packages/b", "target": "pypi",
             "version": "1.0.0", "releasable": False},
        ])
        graph = WorkspaceGraph(root, projects)
        releasables = [Releasable(name="core")]
        snapshot = generate_snapshot(root, projects, graph, releasables=releasables)

        assert snapshot["packages"]["a"]["releasable"] == "core"
        assert snapshot["packages"]["b"]["releasable"] is None

    def test_explicit_releasable_package_field(self, tmp_path):
        """Package with releasable field gets it in snapshot."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0", "releasable": "alpha"},
        ])
        graph = WorkspaceGraph(root, projects)
        releasables = [Releasable(name="alpha")]
        snapshot = generate_snapshot(root, projects, graph, releasables=releasables)

        assert snapshot["packages"]["alpha"]["releasable"] == "alpha"

    def test_dev_node_package_releasable_null(self, tmp_path):
        """dev_node packages get releasable=null in snapshot."""
        root, projects = _make_workspace_with_targets(tmp_path, [
            {"name": "tests", "path": "packages/tests", "target": "pypi",
             "version": "0.1.0", "dev_node": True},
        ])
        graph = WorkspaceGraph(root, projects)
        releasables = []
        snapshot = generate_snapshot(root, projects, graph, releasables=releasables)

        assert snapshot["packages"]["tests"]["releasable"] is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_releasable_with_only_false_projects(self, tmp_project):
        """All projects can be releasable=false if there are releasables defined."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "core"

[[projects]]
path = "a"
name = "a"
releasable = false
""")
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "core"
        # core has no members
        projects = load_workspace(str(tmp_project))
        assert members_of("core", projects) == []

    def test_releasable_with_special_characters_in_name(self, tmp_project):
        """Releasable names with dashes and underscores work."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "my-cool_rel"

[[projects]]
path = "a"
name = "a"
releasable = "my-cool_rel"
""")
        releasables = load_releasables(str(tmp_project))
        assert releasables[0].name == "my-cool_rel"

    def test_load_releasables_reads_file_twice(self, tmp_project):
        """load_releasables without projects reads workspace.toml independently."""
        _write_workspace(tmp_project, """\
[[releasables]]
name = "alpha"

[[projects]]
path = "a"
name = "alpha"
releasable = "alpha"
""")
        # Call without pre-loaded projects
        releasables = load_releasables(str(tmp_project))
        assert len(releasables) == 1
        assert releasables[0].name == "alpha"
