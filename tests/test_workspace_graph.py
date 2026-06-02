"""Tests for rlsbl.workspace_graph."""

import json
import os
import textwrap

import pytest

from rlsbl.workspace_graph import CycleError, Dependency, WorkspaceGraph


def _make_workspace(tmp_path, projects, manifests=None):
    """Helper: create workspace structure and return (root, project_list).

    projects: list of dicts with at least "path" and "name".
    manifests: dict mapping project name to (filename, content) pairs.
    """
    for proj in projects:
        proj_dir = tmp_path / proj["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)

    manifests = manifests or {}
    for proj_name, (filename, content) in manifests.items():
        # Find the project path for this name
        for proj in projects:
            if proj["name"] == proj_name:
                filepath = tmp_path / proj["path"] / filename
                filepath.write_text(content)
                break

    return str(tmp_path), projects


class TestEmptyWorkspace:
    """Empty workspace with no projects."""

    def test_no_projects(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [])
        graph = WorkspaceGraph(root, projects)
        assert graph.topological_order() == []
        assert not graph.has_cycles()


class TestSingleProjectNoDeps:
    """Single project with no dependencies (leaf node)."""

    def test_leaf_node(self, tmp_path):
        projects = [{"path": "packages/alpha", "name": "alpha"}]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("alpha") == []
        assert graph.dependents("alpha") == []
        assert graph.topological_order() == ["alpha"]
        assert graph.dep_count("alpha") == 0
        assert graph.rdep_count("alpha") == 0


class TestPyPIVersionedDep:
    """PyPI workspace: project A depends on project B (versioned dep)."""

    def test_versioned_dep(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b>=1.0"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"
        assert deps[0].dep_type == "versioned"
        assert deps[0].constraint == ">=1.0"


class TestPyPIPathDep:
    """PyPI workspace: project A depends on project B (path dep)."""

    def test_path_dep(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b @ file:///workspace/packages/b"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"
        assert deps[0].dep_type == "path"


class TestPyPINormalization:
    """PyPI normalization: my-core matches workspace project my_core."""

    def test_hyphen_underscore_match(self, tmp_path):
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/my_core", "name": "my_core"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["my-core>=1.0"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0].name == "my_core"

    def test_case_insensitive_match(self, tmp_path):
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/MyLib", "name": "MyLib"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["mylib"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0].name == "MyLib"

    def test_dots_equivalent(self, tmp_path):
        """Dots, hyphens, and underscores are equivalent per PEP 503."""
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/my.lib", "name": "my.lib"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["my_lib>=0.1"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0].name == "my.lib"


class TestNpmWorkspaceDep:
    """npm workspace: project A depends on B via workspace:*."""

    def test_workspace_star(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "dependencies": {"b": "workspace:*"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"
        assert deps[0].dep_type == "workspace"
        assert deps[0].constraint == "workspace:*"

    def test_workspace_caret(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "dependencies": {"b": "workspace:^"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].dep_type == "workspace"

    def test_workspace_tilde(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "devDependencies": {"b": "workspace:~"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].dep_type == "workspace"


class TestNpmFileDep:
    """npm workspace: project A depends on B via file:../b."""

    def test_file_dep(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "dependencies": {"b": "file:../b"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"
        assert deps[0].dep_type == "path"
        assert deps[0].constraint == "file:../b"


class TestMixedEcosystem:
    """Mixed ecosystem: pypi project depends on npm project by name match."""

    def test_cross_ecosystem(self, tmp_path):
        projects = [
            {"path": "packages/pypi-app", "name": "pypi-app"},
            {"path": "packages/shared", "name": "shared"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "pypi-app"
            dependencies = ["shared>=0.1"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "pypi-app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("pypi-app")
        assert len(deps) == 1
        assert deps[0].name == "shared"


class TestTopologicalSort:
    """Topological sort: A->B->C returns [C, B, A]."""

    def test_linear_chain(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
            {"path": "packages/c", "name": "c"},
        ]
        # A depends on B, B depends on C
        pyproject_a = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b"]
        """)
        pyproject_b = textwrap.dedent("""\
            [project]
            name = "b"
            dependencies = ["c"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject_a),
            "b": ("pyproject.toml", pyproject_b),
        })
        graph = WorkspaceGraph(root, projects)
        order = graph.topological_order()
        assert order == ["c", "b", "a"]

    def test_diamond_shape(self, tmp_path):
        """A depends on B and C, both B and C depend on D."""
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
            {"path": "packages/c", "name": "c"},
            {"path": "packages/d", "name": "d"},
        ]
        pyproject_a = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b", "c"]
        """)
        pyproject_b = textwrap.dedent("""\
            [project]
            name = "b"
            dependencies = ["d"]
        """)
        pyproject_c = textwrap.dedent("""\
            [project]
            name = "c"
            dependencies = ["d"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject_a),
            "b": ("pyproject.toml", pyproject_b),
            "c": ("pyproject.toml", pyproject_c),
        })
        graph = WorkspaceGraph(root, projects)
        order = graph.topological_order()
        # d must come before b and c, both before a
        assert order.index("d") < order.index("b")
        assert order.index("d") < order.index("c")
        assert order.index("b") < order.index("a")
        assert order.index("c") < order.index("a")


class TestCycleDetection:
    """Cycle detection: A->B->A raises CycleError."""

    def test_simple_cycle(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pyproject_a = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b"]
        """)
        pyproject_b = textwrap.dedent("""\
            [project]
            name = "b"
            dependencies = ["a"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject_a),
            "b": ("pyproject.toml", pyproject_b),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.has_cycles()
        with pytest.raises(CycleError):
            graph.topological_order()

    def test_three_node_cycle(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
            {"path": "packages/c", "name": "c"},
        ]
        pyproject_a = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b"]
        """)
        pyproject_b = textwrap.dedent("""\
            [project]
            name = "b"
            dependencies = ["c"]
        """)
        pyproject_c = textwrap.dedent("""\
            [project]
            name = "c"
            dependencies = ["a"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject_a),
            "b": ("pyproject.toml", pyproject_b),
            "c": ("pyproject.toml", pyproject_c),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.has_cycles()


class TestNoManifest:
    """Project with no manifest: treated as leaf."""

    def test_no_manifest_is_leaf(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        # a has a manifest that depends on b, but b has no manifest
        pyproject_a = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["b"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject_a),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("b") == []
        assert graph.dep_count("b") == 0
        # b is still a dependency of a
        assert graph.dependents("b") == ["a"]


class TestOptionalDependencies:
    """Optional dependencies (pypi): deps in optional-dependencies are included."""

    def test_optional_deps_included(self, tmp_path):
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/core", "name": "core"},
            {"path": "packages/extras", "name": "extras"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core"]

            [project.optional-dependencies]
            dev = ["extras>=0.1"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        dep_names = {d.name for d in deps}
        assert "core" in dep_names
        assert "extras" in dep_names
        assert len(deps) == 2

    def test_multiple_optional_groups(self, tmp_path):
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/test-lib", "name": "test-lib"},
            {"path": "packages/docs-lib", "name": "docs-lib"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"

            [project.optional-dependencies]
            test = ["test-lib"]
            docs = ["docs-lib>=2.0"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        dep_names = {d.name for d in deps}
        assert "test-lib" in dep_names
        assert "docs-lib" in dep_names


class TestDependents:
    """Verify reverse lookup is correct."""

    def test_dependents_multiple(self, tmp_path):
        projects = [
            {"path": "packages/core", "name": "core"},
            {"path": "packages/app1", "name": "app1"},
            {"path": "packages/app2", "name": "app2"},
        ]
        pyproject_app1 = textwrap.dedent("""\
            [project]
            name = "app1"
            dependencies = ["core"]
        """)
        pyproject_app2 = textwrap.dedent("""\
            [project]
            name = "app2"
            dependencies = ["core"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app1": ("pyproject.toml", pyproject_app1),
            "app2": ("pyproject.toml", pyproject_app2),
        })
        graph = WorkspaceGraph(root, projects)
        dependents = sorted(graph.dependents("core"))
        assert dependents == ["app1", "app2"]
        assert graph.dependents("app1") == []

    def test_no_self_dep(self, tmp_path):
        """A project listing itself as a dep should not create self-edge."""
        projects = [
            {"path": "packages/a", "name": "a"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "a"
            dependencies = ["a"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("a") == []
        assert graph.dependents("a") == []


class TestCounts:
    """dep_count() and rdep_count() return correct numbers."""

    def test_counts(self, tmp_path):
        projects = [
            {"path": "packages/core", "name": "core"},
            {"path": "packages/utils", "name": "utils"},
            {"path": "packages/app", "name": "app"},
        ]
        pyproject_app = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core", "utils"]
        """)
        pyproject_utils = textwrap.dedent("""\
            [project]
            name = "utils"
            dependencies = ["core"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject_app),
            "utils": ("pyproject.toml", pyproject_utils),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.dep_count("app") == 2
        assert graph.dep_count("utils") == 1
        assert graph.dep_count("core") == 0
        assert graph.rdep_count("core") == 2
        assert graph.rdep_count("utils") == 1
        assert graph.rdep_count("app") == 0


class TestNpmVersionedDep:
    """npm: versioned dep matching a workspace name."""

    def test_npm_versioned(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "dependencies": {"b": "^1.0.0"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].dep_type == "versioned"
        assert deps[0].constraint == "^1.0.0"


class TestNpmPeerDependencies:
    """npm: peerDependencies are also scanned."""

    def test_peer_dep(self, tmp_path):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        pkg_json = json.dumps({
            "name": "a",
            "peerDependencies": {"b": ">=1.0.0"},
        })
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", pkg_json),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"


class TestMalformedManifest:
    """Malformed manifest is skipped with a warning."""

    def test_malformed_pyproject(self, tmp_path, capsys):
        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("pyproject.toml", "this is not valid toml [[["),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("a") == []
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_malformed_package_json(self, tmp_path, capsys):
        projects = [
            {"path": "packages/a", "name": "a"},
        ]
        root, projects = _make_workspace(tmp_path, projects, {
            "a": ("package.json", "{not valid json"),
        })
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("a") == []
        captured = capsys.readouterr()
        assert "Warning" in captured.err


class TestDeduplication:
    """Same dep appearing in both main and optional should only appear once."""

    def test_dedup_across_sections(self, tmp_path):
        projects = [
            {"path": "packages/app", "name": "app"},
            {"path": "packages/core", "name": "core"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core>=1.0"]

            [project.optional-dependencies]
            dev = ["core>=2.0"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0].name == "core"


class TestExplicitDependsOn:
    """Tests for explicit depends_on edges from workspace config."""

    def test_explicit_edge_created(self, tmp_path):
        """Project A with depends_on: ["B"] creates an explicit edge."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": ["b"]},
            {"path": "packages/b", "name": "b"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("a")
        assert len(deps) == 1
        assert deps[0].name == "b"
        assert deps[0].dep_type == "explicit"
        assert deps[0].constraint == ""

    def test_explicit_dep_type(self, tmp_path):
        """Explicit edge appears in dependencies() with dep_type='explicit'."""
        projects = [
            {"path": "packages/app", "name": "app", "depends_on": ["lib"]},
            {"path": "packages/lib", "name": "lib"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0] == Dependency(name="lib", dep_type="explicit", constraint="", scope="explicit")

    def test_explicit_counted_in_dep_rdep(self, tmp_path):
        """Explicit edge counted in dep_count/rdep_count."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": ["b"]},
            {"path": "packages/b", "name": "b"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.dep_count("a") == 1
        assert graph.rdep_count("b") == 1
        assert graph.dep_count("b") == 0
        assert graph.rdep_count("a") == 0

    def test_topological_sort_respects_explicit(self, tmp_path):
        """Topological sort respects explicit edges (B before A)."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": ["b"]},
            {"path": "packages/b", "name": "b", "depends_on": ["c"]},
            {"path": "packages/c", "name": "c"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        order = graph.topological_order()
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    def test_invalid_depends_on_raises(self, tmp_path):
        """Invalid depends_on name raises ValueError with clear message."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": ["nonexistent"]},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        with pytest.raises(ValueError, match="no workspace project with that name exists"):
            WorkspaceGraph(root, projects)

    def test_self_reference_silently_skipped(self, tmp_path):
        """Self-reference in depends_on is silently skipped."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": ["a"]},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("a") == []
        assert graph.dependents("a") == []

    def test_explicit_and_auto_detected_coexist(self, tmp_path):
        """Explicit + auto-detected deps coexist."""
        projects = [
            {"path": "packages/app", "name": "app", "depends_on": ["infra"]},
            {"path": "packages/core", "name": "core"},
            {"path": "packages/infra", "name": "infra"},
        ]
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core>=1.0"]
        """)
        root, projects = _make_workspace(tmp_path, projects, {
            "app": ("pyproject.toml", pyproject),
        })
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("app")
        dep_names = {d.name for d in deps}
        assert dep_names == {"core", "infra"}
        # Verify types
        dep_by_name = {d.name: d for d in deps}
        assert dep_by_name["core"].dep_type == "versioned"
        assert dep_by_name["infra"].dep_type == "explicit"

    def test_empty_depends_on_harmless(self, tmp_path):
        """depends_on with empty list is harmless."""
        projects = [
            {"path": "packages/a", "name": "a", "depends_on": []},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("a") == []
        assert graph.topological_order() == ["a"]
