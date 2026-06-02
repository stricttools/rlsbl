"""Integration tests for cross-registry depends_on edges.

Verifies that explicit depends_on edges work correctly across different
package registries (npm and pypi) in all graph-consuming operations:
topological ordering, dependency counting, dependency listing, and
coexistence with auto-detected edges.
"""

import json
import textwrap

from rlsbl.workspace_graph import Dependency, WorkspaceGraph


def _make_cross_registry_workspace(tmp_path):
    """Build a workspace with 3 projects spanning npm and pypi.

    Layout:
      - lib-core (npm, package.json) -- no deps
      - orchestrator (pypi, pyproject.toml) -- explicit dep on lib-core,
        auto-detected dep on cli-utils (a sibling pypi project)
      - cli (pypi, pyproject.toml) -- auto-detected dep on orchestrator

    Returns (root, projects) suitable for WorkspaceGraph().
    """
    projects = [
        {"path": "packages/lib-core", "name": "lib-core"},
        {
            "path": "packages/orchestrator",
            "name": "orchestrator",
            "depends_on": ["lib-core"],
        },
        {"path": "packages/cli", "name": "cli"},
        {"path": "packages/cli-utils", "name": "cli-utils"},
    ]

    for proj in projects:
        (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)

    # lib-core: npm project with no deps
    (tmp_path / "packages/lib-core/package.json").write_text(
        json.dumps({"name": "lib-core", "version": "1.0.0"})
    )

    # cli-utils: pypi project with no deps (exists so orchestrator
    # can have both an explicit and an auto-detected dep)
    (tmp_path / "packages/cli-utils/pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "cli-utils"
        """)
    )

    # orchestrator: pypi project, auto-detected dep on cli-utils,
    # explicit dep on lib-core (cross-registry)
    (tmp_path / "packages/orchestrator/pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "orchestrator"
            dependencies = ["cli-utils>=0.1"]
        """)
    )

    # cli: pypi project, auto-detected dep on orchestrator
    (tmp_path / "packages/cli/pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "cli"
            dependencies = ["orchestrator>=1.0"]
        """)
    )

    return str(tmp_path), projects


class TestReleaseOrderRespectsExplicitDeps:
    """topological_order() respects cross-registry explicit deps."""

    def test_release_order_respects_explicit_deps(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        order = graph.topological_order()

        assert order.index("lib-core") < order.index("orchestrator")
        assert order.index("orchestrator") < order.index("cli")

    def test_lib_core_is_a_leaf(self, tmp_path):
        """lib-core has no intra-workspace deps of its own."""
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        assert graph.dependencies("lib-core") == []


class TestStatusCountsExplicitDeps:
    """dep_count / rdep_count reflect explicit edges correctly."""

    def test_lib_core_rdep_count(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        # orchestrator depends on lib-core (explicit)
        assert graph.rdep_count("lib-core") == 1

    def test_orchestrator_dep_count(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        # orchestrator: 1 explicit (lib-core) + 1 auto-detected (cli-utils)
        assert graph.dep_count("orchestrator") == 2

    def test_cli_dep_count(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        # cli: 1 auto-detected (orchestrator)
        assert graph.dep_count("cli") == 1

    def test_cli_utils_counts(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        assert graph.dep_count("cli-utils") == 0
        assert graph.rdep_count("cli-utils") == 1


class TestOutdatedShowsExplicitDeps:
    """dependencies() reports explicit deps with dep_type='explicit'."""

    def test_explicit_dep_on_lib_core(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("orchestrator")
        dep_by_name = {d.name: d for d in deps}
        assert "lib-core" in dep_by_name
        assert dep_by_name["lib-core"].dep_type == "explicit"
        assert dep_by_name["lib-core"].constraint == ""

    def test_auto_detected_dep_on_cli_utils(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("orchestrator")
        dep_by_name = {d.name: d for d in deps}
        assert "cli-utils" in dep_by_name
        assert dep_by_name["cli-utils"].dep_type == "versioned"
        assert dep_by_name["cli-utils"].constraint == ">=0.1"

    def test_cli_auto_detected_dep(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("cli")
        assert len(deps) == 1
        assert deps[0] == Dependency(
            name="orchestrator", dep_type="versioned", constraint=">=1.0", scope="runtime"
        )


class TestMixedExplicitAndAutoDetected:
    """Explicit and auto-detected edges coexist without interference."""

    def test_both_edge_types_present(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("orchestrator")
        types = {d.dep_type for d in deps}
        assert "explicit" in types
        assert "versioned" in types

    def test_no_duplicate_edges(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("orchestrator")
        names = [d.name for d in deps]
        # Each dep appears exactly once
        assert len(names) == len(set(names))

    def test_deduplication_auto_wins_over_explicit(self, tmp_path):
        """When a project has both auto-detected and explicit dep on the
        same target, auto-detected (scanned first) wins due to first-wins
        dedup in WorkspaceGraph.__init__."""
        projects = [
            {
                "path": "packages/app",
                "name": "app",
                "depends_on": ["core"],
            },
            {"path": "packages/core", "name": "core"},
        ]
        (tmp_path / "packages/app").mkdir(parents=True)
        (tmp_path / "packages/core").mkdir(parents=True)
        (tmp_path / "packages/app/pyproject.toml").write_text(
            textwrap.dedent("""\
                [project]
                name = "app"
                dependencies = ["core>=1.0"]
            """)
        )

        graph = WorkspaceGraph(str(tmp_path), projects)
        deps = graph.dependencies("app")
        assert len(deps) == 1
        # Auto-detected (versioned) was found first, so it wins
        assert deps[0].dep_type == "versioned"
        assert deps[0].name == "core"

    def test_reverse_deps_include_explicit(self, tmp_path):
        """dependents() includes projects connected via explicit edges."""
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        assert "orchestrator" in graph.dependents("lib-core")

    def test_full_graph_no_cycles(self, tmp_path):
        root, projects = _make_cross_registry_workspace(tmp_path)
        graph = WorkspaceGraph(root, projects)
        assert not graph.has_cycles()
