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

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "packages" in data
        assert "edges" in data
        assert set(data["packages"].keys()) == {"models", "schema", "app"}

        # Check models package
        models = data["packages"]["models"]
        assert "schema" in models["deps"]
        assert "app" in models["rdeps"]
        assert models["targets"] == ["npm"]
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

        _cmd_graph({"format": "json"}, project_root=".")
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

        _cmd_graph({"format": "dot"}, project_root=".")
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

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert output.startswith("digraph dependencies {")
        assert output.endswith("}")
        # No edges
        assert "->" not in output


class TestGraphText:
    """Test text tree output format."""

    def test_correct_indentation(self, mock_git_repo, capsys):
        """Text output shows packages with indented deps and fact labels."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        # A is a leaf (no dependents) -- labeled [leaf]
        assert "A [leaf]" in lines
        a_idx = lines.index("A [leaf]")
        assert lines[a_idx + 1] == "  B"
        assert lines[a_idx + 2] == "    C"

        # B with its deps (B has dependents so no [leaf])
        b_top_idx = None
        for i, line in enumerate(lines):
            if line == "B" and i > a_idx:
                b_top_idx = i
                break
        assert b_top_idx is not None
        assert lines[b_top_idx + 1] == "  C"

        # C standalone (has dependents, no [leaf])
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

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l.strip()]

        # Both are leaves (no dependents)
        assert lines == ["alpha [leaf]", "zeta [leaf]"]


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

        _cmd_graph({"format": "json", "root": "A"}, project_root=".")
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
            _cmd_graph({"format": "json", "root": "nonexistent"}, project_root=".")
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

        _cmd_graph({"format": "json", "reverse": "C"}, project_root=".")
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

        _cmd_graph({"format": "json", "root": "A", "depth": 1}, project_root=".")
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

        _cmd_graph({"format": "json", "reverse": "C", "depth": 1}, project_root=".")
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
        _cmd_graph({"format": "json", "output": output_path}, project_root=".")

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
        _cmd_graph({"format": "dot", "output": output_path}, project_root=".")

        assert os.path.isfile(output_path)
        with open(output_path) as f:
            content = f.read()
        assert "digraph dependencies {" in content
        assert '"P" -> "Q";' in content


class TestGraphEdgeCases:
    """Edge cases for the graph command."""

    def test_empty_workspace(self, mock_git_repo, capsys):
        """Empty workspace prints message and returns."""
        _cmd_init({}, project_root=".")
        capsys.readouterr()

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." in captured.out

    def test_no_workspace(self, mock_git_repo):
        """No workspace should error and exit 1."""
        with pytest.raises(SystemExit):
            _cmd_graph({"format": "json"}, project_root=".")


def _make_npm_project_full(base_path, subdir, version="0.1.0", deps=None,
                           dev_deps=None, peer_deps=None):
    """Create an npm project with optional dependencies, devDependencies, and peerDependencies."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    if deps:
        pkg["dependencies"] = deps
    if dev_deps:
        pkg["devDependencies"] = dev_deps
    if peer_deps:
        pkg["peerDependencies"] = peer_deps
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)


class TestRawFacts:
    """Test raw facts (dev_node, library, has_runtime_dependents, is_leaf) in graph output."""

    def test_json_includes_raw_facts(self, mock_git_repo, capsys):
        """JSON output includes dev_node, library, has_runtime_dependents, is_leaf."""
        _make_npm_project(mock_git_repo, "core", deps={"utils": "^1.0.0"})
        _make_npm_project(mock_git_repo, "utils")

        projects = [
            {"path": "core", "name": "core"},
            {"path": "utils", "name": "utils"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        for name in ("core", "utils"):
            pkg = data["packages"][name]
            assert "dev_node" in pkg
            assert "library" in pkg
            assert "has_runtime_dependents" in pkg
            assert "is_leaf" in pkg

    def test_leaf_detection(self, mock_git_repo, capsys):
        """Package with no dependents is marked is_leaf=True."""
        _make_npm_project(mock_git_repo, "app", deps={"lib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "lib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "lib", "name": "lib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # app has no dependents -> is_leaf
        assert data["packages"]["app"]["is_leaf"] is True
        # lib has app as dependent -> not is_leaf
        assert data["packages"]["lib"]["is_leaf"] is False

    def test_has_runtime_dependents(self, mock_git_repo, capsys):
        """Package depended on via runtime scope has has_runtime_dependents=True."""
        _make_npm_project(mock_git_repo, "app", deps={"lib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "lib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "lib", "name": "lib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # lib is depended on by app via runtime scope
        assert data["packages"]["lib"]["has_runtime_dependents"] is True
        # app has no dependents
        assert data["packages"]["app"]["has_runtime_dependents"] is False

    def test_dev_only_dependents_not_runtime(self, mock_git_repo, capsys):
        """Package depended on only via dev scope has has_runtime_dependents=False."""
        _make_npm_project_full(mock_git_repo, "app", dev_deps={"testlib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "testlib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "testlib", "name": "testlib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # testlib is depended on by app via dev scope only
        assert data["packages"]["testlib"]["has_runtime_dependents"] is False
        assert data["packages"]["testlib"]["is_leaf"] is False  # still has dependents

    def test_explicit_depends_on_is_runtime(self, mock_git_repo, capsys):
        """Explicit depends_on in workspace config counts as runtime-like."""
        _make_npm_project(mock_git_repo, "app")
        _make_npm_project(mock_git_repo, "core")

        projects = [
            {"path": "app", "name": "app", "depends_on": ["core"]},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # core is depended on by app via explicit scope
        assert data["packages"]["core"]["has_runtime_dependents"] is True

    def test_dev_node_flag(self, mock_git_repo, capsys):
        """dev_node flag from project config is reflected in output."""
        _make_npm_project(mock_git_repo, "devtool")
        _make_npm_project(mock_git_repo, "main")

        projects = [
            {"path": "devtool", "name": "devtool", "dev_node": True},
            {"path": "main", "name": "main"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["packages"]["devtool"]["dev_node"] is True
        assert data["packages"]["main"]["dev_node"] is False

    def test_library_flag(self, mock_git_repo, capsys):
        """library flag from project config is reflected in output."""
        _make_npm_project(mock_git_repo, "shared-lib")
        _make_npm_project(mock_git_repo, "app")

        projects = [
            {"path": "shared-lib", "name": "shared-lib", "library": True},
            {"path": "app", "name": "app"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["packages"]["shared-lib"]["library"] is True
        assert data["packages"]["app"]["library"] is False


class TestDOTEdgeStyling:
    """Test DOT output edge styling based on scope."""

    def test_runtime_edge_default(self, mock_git_repo, capsys):
        """Runtime edges have no extra attributes (default solid black)."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        # Runtime edge: no extra attributes
        assert '"A" -> "B";' in output

    def test_dev_edge_dashed(self, mock_git_repo, capsys):
        """Dev edges have dashed gray style."""
        _make_npm_project_full(mock_git_repo, "A", dev_deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert '"A" -> "B" [style=dashed, color=gray];' in output

    def test_peer_edge_dotted(self, mock_git_repo, capsys):
        """Peer edges have dotted blue style."""
        _make_npm_project_full(mock_git_repo, "A", peer_deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert '"A" -> "B" [style=dotted, color=blue];' in output

    def test_explicit_edge_thick(self, mock_git_repo, capsys):
        """Explicit edges have thick black style."""
        _make_npm_project(mock_git_repo, "A")
        _make_npm_project(mock_git_repo, "B")

        projects = [
            {"path": "A", "name": "A", "depends_on": ["B"]},
            {"path": "B", "name": "B"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert '"A" -> "B" [color=black, penwidth=2];' in output


class TestDOTNodeStyling:
    """Test DOT output node styling based on raw facts."""

    def test_dev_node_gray(self, mock_git_repo, capsys):
        """dev_node packages get gray fill in DOT."""
        _make_npm_project(mock_git_repo, "devtool")
        _make_npm_project(mock_git_repo, "main")

        projects = [
            {"path": "devtool", "name": "devtool", "dev_node": True},
            {"path": "main", "name": "main"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        assert '"devtool" [style=filled, fillcolor=lightgray];' in output

    def test_leaf_node_green(self, mock_git_repo, capsys):
        """Non-dev leaf packages get green fill in DOT."""
        _make_npm_project(mock_git_repo, "app", deps={"lib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "lib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "lib", "name": "lib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        # app is a leaf (no dependents)
        assert '"app" [style=filled, fillcolor=lightgreen];' in output
        # lib has dependents, not a leaf -- no node styling
        assert '"lib" [style=filled' not in output

    def test_dev_node_overrides_leaf(self, mock_git_repo, capsys):
        """dev_node styling takes precedence over leaf styling."""
        _make_npm_project(mock_git_repo, "devtool")

        projects = [
            {"path": "devtool", "name": "devtool", "dev_node": True},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        # dev_node + is_leaf -> dev_node wins
        assert '"devtool" [style=filled, fillcolor=lightgray];' in output
        assert 'lightgreen' not in output

    def test_interior_node_no_styling(self, mock_git_repo, capsys):
        """Non-dev interior nodes (have dependents) get no special styling."""
        _make_npm_project(mock_git_repo, "A", deps={"B": "^1.0.0"})
        _make_npm_project(mock_git_repo, "B", deps={"C": "^1.0.0"})
        _make_npm_project(mock_git_repo, "C")

        projects = [
            {"path": "A", "name": "A"},
            {"path": "B", "name": "B"},
            {"path": "C", "name": "C"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "dot"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        # B and C have dependents -- no node styling line
        assert '"B" [style=filled' not in output
        assert '"C" [style=filled' not in output
        # A is a leaf -- gets green
        assert '"A" [style=filled, fillcolor=lightgreen];' in output


class TestTextLabels:
    """Test text output fact labels."""

    def test_dev_label(self, mock_git_repo, capsys):
        """dev_node packages show [dev] label in text output."""
        _make_npm_project(mock_git_repo, "devtool")

        projects = [
            {"path": "devtool", "name": "devtool", "dev_node": True},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        assert "devtool [dev] [leaf]" in lines

    def test_lib_label(self, mock_git_repo, capsys):
        """library packages show [lib] label in text output."""
        _make_npm_project(mock_git_repo, "utils", deps={"core": "^1.0.0"})
        _make_npm_project(mock_git_repo, "core")

        projects = [
            {"path": "utils", "name": "utils", "library": True},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        # utils is a leaf + library
        assert "utils [lib] [leaf]" in lines

    def test_leaf_label(self, mock_git_repo, capsys):
        """Leaf packages (no dependents) show [leaf] label."""
        _make_npm_project(mock_git_repo, "app", deps={"lib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "lib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "lib", "name": "lib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        # app is a leaf
        assert "app [leaf]" in lines
        # lib has dependents -- no [leaf] label
        assert any(line.strip() == "lib" for line in lines)

    def test_no_labels_for_interior(self, mock_git_repo, capsys):
        """Interior non-dev non-lib packages have no labels."""
        _make_npm_project(mock_git_repo, "app", deps={"mid": "^1.0.0"})
        _make_npm_project(mock_git_repo, "mid", deps={"core": "^1.0.0"})
        _make_npm_project(mock_git_repo, "core")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "mid", "name": "mid"},
            {"path": "core", "name": "core"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")

        # mid and core have dependents, no special flags
        top_level_mid = [l for l in lines if l == "mid"]
        assert len(top_level_mid) > 0

    def test_subtree_labels(self, mock_git_repo, capsys):
        """Labels appear on subtree (indented) nodes too."""
        _make_npm_project(mock_git_repo, "app", deps={"leaf-lib": "^1.0.0"})
        _make_npm_project(mock_git_repo, "leaf-lib")

        projects = [
            {"path": "app", "name": "app"},
            {"path": "leaf-lib", "name": "leaf-lib"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "text"}, project_root=".")
        captured = capsys.readouterr()
        output = captured.out.strip()

        # leaf-lib appears as a subtree under app -- but it has dependents (app),
        # so no [leaf] label
        assert "  leaf-lib" in output
