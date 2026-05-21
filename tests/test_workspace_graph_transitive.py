"""Tests for WorkspaceGraph.transitive_deps() and transitive_rdeps()."""

import pytest

from rlsbl.workspace_graph import WorkspaceGraph


def _make_workspace(tmp_path, projects):
    """Create workspace directories and return (root, projects).

    Uses depends_on for edges so no manifest files are needed.
    """
    for proj in projects:
        (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)
    return str(tmp_path), projects


class TestTransitiveDeps:
    """transitive_deps() -- forward (dependency) direction."""

    def test_diamond(self, tmp_path):
        """A->B->D, A->C->D: returns [B, C, D] (alpha-sorted siblings)."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B", "C"]},
            {"path": "p/b", "name": "B", "depends_on": ["D"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A") == ["B", "C", "D"]

    def test_linear_chain_unlimited(self, tmp_path):
        """A->B->C->D: returns [B, C, D]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B", "depends_on": ["C"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A") == ["B", "C", "D"]

    def test_linear_chain_depth_1(self, tmp_path):
        """A->B->C->D with depth=1: returns [B]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B", "depends_on": ["C"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A", depth=1) == ["B"]

    def test_linear_chain_depth_2(self, tmp_path):
        """A->B->C->D with depth=2: returns [B, C]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B", "depends_on": ["C"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A", depth=2) == ["B", "C"]

    def test_isolated_node(self, tmp_path):
        """Node with no deps returns []."""
        projects = [
            {"path": "p/a", "name": "A"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A") == []

    def test_depth_zero(self, tmp_path):
        """depth=0 always returns []."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_deps("A", depth=0) == []

    def test_unknown_name_raises(self, tmp_path):
        """Unknown project name raises KeyError."""
        projects = [{"path": "p/a", "name": "A"}]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        with pytest.raises(KeyError):
            graph.transitive_deps("nonexistent")


class TestTransitiveRdeps:
    """transitive_rdeps() -- reverse (dependent) direction."""

    def test_diamond(self, tmp_path):
        """A->B->D, A->C->D: rdeps of D are [B, C, A]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B", "C"]},
            {"path": "p/b", "name": "B", "depends_on": ["D"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_rdeps("D") == ["B", "C", "A"]

    def test_leaf_node_no_rdeps(self, tmp_path):
        """Leaf node (no rdeps) returns []."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        # A has no dependents
        assert graph.transitive_rdeps("A") == []

    def test_depth_zero(self, tmp_path):
        """depth=0 always returns []."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_rdeps("B", depth=0) == []

    def test_unknown_name_raises(self, tmp_path):
        """Unknown project name raises KeyError."""
        projects = [{"path": "p/a", "name": "A"}]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        with pytest.raises(KeyError):
            graph.transitive_rdeps("nonexistent")

    def test_linear_chain_rdeps(self, tmp_path):
        """A->B->C->D: rdeps of D are [C, B, A]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B", "depends_on": ["C"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_rdeps("D") == ["C", "B", "A"]

    def test_linear_chain_rdeps_depth_1(self, tmp_path):
        """A->B->C->D: rdeps of D at depth=1 are [C]."""
        projects = [
            {"path": "p/a", "name": "A", "depends_on": ["B"]},
            {"path": "p/b", "name": "B", "depends_on": ["C"]},
            {"path": "p/c", "name": "C", "depends_on": ["D"]},
            {"path": "p/d", "name": "D"},
        ]
        root, projects = _make_workspace(tmp_path, projects)
        graph = WorkspaceGraph(root, projects)
        assert graph.transitive_rdeps("D", depth=1) == ["C"]
