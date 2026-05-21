"""Tests for monorepo graph subcommand."""

import json
import os

import pytest

from rlsbl.commands.monorepo import _cmd_graph, _cmd_init
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


def _make_npm_project(base_path, subdir, version="0.1.0", deps=None):
    """Create a minimal npm project with optional dependencies."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    if deps:
        pkg["dependencies"] = deps
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)


def _init_workspace(base_path, projects):
    """Initialize a workspace with the given project list."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


class TestGraphJSON:
    """Test JSON output format."""

    def test_three_package_graph(self, mock_git_repo, capsys):
        """JSON output with 3 packages has correct structure."""
        _make_npm_project(mock_git_repo, "models", version="1.2.0", deps={"schema": "^1.0.0"})
        _make_npm_project(mock_git_repo, "schema", version="1.0.0")
        _make_npm_project(mock_git_repo, "app", version="2.0.0", deps={"models": "^1.0.0"})

        projects = [
            {"path": "models", "name": "models"},
            {"path": "schema", "name": "schema"},
            {"path": "app", "name": "app"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "packages" in data
        assert "edges" in data
        assert set(data["packages"].keys()) == {"models", "schema", "app"}

        # Check models package
        models = data["packages"]["models"]
        assert "schema" in models["deps"]
        assert "app" in models["rdeps"]
        assert models["target"] == "npm"
        assert models["version"] == "1.2.0"

        # Check schema package
        schema = data["packages"]["schema"]
        assert schema["deps"] == []
        assert "models" in schema["rdeps"]

        # Check app package
        app_pkg = data["packages"]["app"]
        assert "models" in app_pkg["deps"]
        assert app_pkg["rdeps"] == []

        # Check edges
        edge_pairs = [(e["from"], e["to"]) for e in data["edges"]]
        assert ("models", "schema") in edge_pairs
        assert ("app", "models") in edge_pairs

        # Check edge details
        models_to_schema = [e for e in data["edges"] if e["from"] == "models" and e["to"] == "schema"][0]
        assert models_to_schema["type"] == "versioned"
        assert models_to_schema["constraint"] == "^1.0.0"

    def test_no_deps_graph(self, mock_git_repo, capsys):
        """Graph with no dependencies still lists all packages."""
        _make_npm_project(mock_git_repo, "alpha", version="1.0.0")
        _make_npm_project(mock_git_repo, "beta", version="2.0.0")

        projects = [
            {"path": "alpha", "name": "alpha"},
            {"path": "beta", "name": "beta"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert set(data["packages"].keys()) == {"alpha", "beta"}
        assert data["edges"] == []
        assert data["packages"]["alpha"]["deps"] == []
        assert data["packages"]["beta"]["deps"] == []


class TestGraphDOT:
    """Test DOT output format."""

    def test_valid_dot_syntax(self, mock_git_repo, capsys):
        """DOT output has correct Graphviz syntax."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"})
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert output.startswith("digraph dependencies {")
        assert output.endswith("}")
        assert "rankdir=TB" in output
        assert 'node [shape=box' in output
        assert '"A" -> "B";' in output
        assert '"B" -> "C";' in output

    def test_dot_no_deps(self, mock_git_repo, capsys):
        """DOT output with no edges still has valid structure."""
        _make_npm_project(mock_git_repo, "solo")

        projects = [{"path": "solo", "name": "solo"}]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"})
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert output.startswith("digraph dependencies {")
        assert output.endswith("}")
        # No edges
        assert "->" not in output


class TestGraphText:
    """Test text tree output format."""

    def test_correct_indentation(self, mock_git_repo, capsys):
        """Text output shows packages with indented deps."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"})
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        # A with its transitive tree
        assert "A" in lines
        a_idx = lines.index("A")
        assert lines[a_idx + 1] == "  B"
        assert lines[a_idx + 2] == "    C"

        # B with its deps
        b_top_idx = None
        for i, line in enumerate(lines):
            if line == "B" and i > a_idx:
                b_top_idx = i
                break
        assert b_top_idx is not None
        assert lines[b_top_idx + 1] == "  C"

        # C standalone (no deps)
        c_top_idx = None
        for i, line in enumerate(lines):
            if line == "C" and i > b_top_idx:
                c_top_idx = i
                break
        assert c_top_idx is not None

    def test_text_no_deps(self, mock_git_repo, capsys):
        """Text output with independent packages shows them sorted."""
        _make_npm_project(mock_git_repo, "zeta")
        _make_npm_project(mock_git_repo, "alpha")

        projects = [
            {"path": "zeta", "name": "zeta"},
            {"path": "alpha", "name": "alpha"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"})
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l.strip()]

        assert lines == ["alpha", "zeta"]


class TestGraphRootFilter:
    """Test --root filtering."""

    def test_root_shows_only_deps(self, mock_git_repo, capsys):
        """--root shows the package and its transitive deps only."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")
        _make_npm_project(mock_git_repo, "D")  # unrelated

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
            {"path": "D", "name": "D"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json", "root": "A"})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert set(data["packages"].keys()) == {"A", "B", "C"}
        assert "D" not in data["packages"]

    def test_root_unknown_package(self, mock_git_repo, capsys):
        """--root with unknown package exits with error."""
        _make_npm_project(mock_git_repo, "A")

        projects = [{"path": "A", "name": "A"}]
        _init_workspace(mock_git_repo, projects)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_graph({"format": "json", "root": "nonexistent"})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "nonexistent" in captured.err


class TestGraphReverseFilter:
    """Test --reverse filtering."""

    def test_reverse_shows_only_rdeps(self, mock_git_repo, capsys):
        """--reverse shows the package and its transitive rdeps only."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")
        _make_npm_project(mock_git_repo, "D")  # unrelated

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
            {"path": "D", "name": "D"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json", "reverse": "C"})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert set(data["packages"].keys()) == {"A", "B", "C"}
        assert "D" not in data["packages"]


class TestGraphDepthLimit:
    """Test --depth limiting."""

    def test_depth_limits_traversal(self, mock_git_repo, capsys):
        """--depth 1 with --root shows only direct deps."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json", "root": "A", "depth": 1})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # depth=1 from A: only direct dep B (not transitive C)
        assert set(data["packages"].keys()) == {"A", "B"}
        assert "C" not in data["packages"]

    def test_depth_reverse(self, mock_git_repo, capsys):
        """--depth 1 with --reverse shows only direct rdeps."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json", "reverse": "C", "depth": 1})
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # depth=1 reverse from C: only direct rdep B (not transitive A)
        assert set(data["packages"].keys()) == {"B", "C"}
        assert "A" not in data["packages"]


class TestGraphOutputFile:
    """Test --output flag writes to file."""

    def test_output_writes_file(self, mock_git_repo, capsys):
        """--output writes graph to a file instead of stdout."""
        _make_npm_project(mock_git_repo, "X", deps={"Y": "^1.0.0"})
        _make_npm_project(mock_git_repo, "Y")

        projects = [
            {"path": "X", "name": "X"},
            {"path": "Y", "name": "Y"},
        ]
        _init_workspace(mock_git_repo, projects)

        output_path = os.path.join(str(mock_git_repo), "graph.json")
        _cmd_graph({"format": "json", "output": output_path})

        assert os.path.isfile(output_path)
        with open(output_path) as f:
            data = json.loads(f.read())
        assert set(data["packages"].keys()) == {"X", "Y"}

        captured = capsys.readouterr()
        assert "Wrote graph to" in captured.out

    def test_output_dot_file(self, mock_git_repo, capsys):
        """--output with DOT format writes valid DOT to file."""
        _make_npm_project(mock_git_repo, "P", deps={"Q": "^1.0.0"})
        _make_npm_project(mock_git_repo, "Q")

        projects = [
            {"path": "P", "name": "P"},
            {"path": "Q", "name": "Q"},
        ]
        _init_workspace(mock_git_repo, projects)

        output_path = os.path.join(str(mock_git_repo), "graph.dot")
        _cmd_graph({"format": "dot", "output": output_path})

        assert os.path.isfile(output_path)
        with open(output_path) as f:
            content = f.read()
        assert "digraph dependencies {" in content
        assert '"P" -> "Q";' in content


class TestGraphEdgeCases:
    """Edge cases for the graph command."""

    def test_empty_workspace(self, mock_git_repo, capsys):
        """Empty workspace prints message and returns."""
        _cmd_init({})
        capsys.readouterr()

        _cmd_graph({"format": "json"})
        captured = capsys.readouterr()
        assert "No projects in workspace." in captured.out

    def test_no_workspace(self, mock_git_repo):
        """No workspace should error and exit 1."""
        with pytest.raises(SystemExit):
            _cmd_graph({"format": "json"})
