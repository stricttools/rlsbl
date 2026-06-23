"""Tests for circular dependency detection (Tarjan's SCC and per-language wrappers)."""

import json
import os
from pathlib import Path

import pytest

from rlsbl.dep_validation import (
    find_circular_deps,
    find_circular_dart_deps,
    find_circular_npm_deps,
    find_circular_python_deps,
)


# ---------------------------------------------------------------------------
# Unit tests: Tarjan's SCC algorithm
# ---------------------------------------------------------------------------


class TestFindCircularDeps:
    """find_circular_deps detects cycles in arbitrary import graphs."""

    def test_no_cycles_empty_graph(self):
        """Empty graph has no cycles."""
        assert find_circular_deps({}) == []

    def test_no_cycles_linear(self):
        """Linear chain A -> B -> C has no cycles."""
        graph = {
            "a": {"b"},
            "b": {"c"},
            "c": set(),
        }
        assert find_circular_deps(graph) == []

    def test_no_cycles_tree(self):
        """Tree structure has no cycles."""
        graph = {
            "root": {"left", "right"},
            "left": {"leaf1"},
            "right": {"leaf2"},
        }
        assert find_circular_deps(graph) == []

    def test_simple_cycle_two_nodes(self):
        """A -> B -> A is detected as a cycle."""
        graph = {
            "a": {"b"},
            "b": {"a"},
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["a", "b"]

    def test_simple_cycle_three_nodes(self):
        """A -> B -> C -> A is detected as a cycle."""
        graph = {
            "a": {"b"},
            "b": {"c"},
            "c": {"a"},
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["a", "b", "c"]

    def test_self_loop_excluded(self):
        """Single-node self-loops are not reported (not interesting)."""
        graph = {
            "a": {"a"},
        }
        assert find_circular_deps(graph) == []

    def test_multiple_independent_cycles(self):
        """Two separate cycles are both detected."""
        graph = {
            "a": {"b"},
            "b": {"a"},
            "x": {"y"},
            "y": {"x"},
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 2
        cycle_sets = [set(c) for c in cycles]
        assert {"a", "b"} in cycle_sets
        assert {"x", "y"} in cycle_sets

    def test_cycle_with_tail(self):
        """A chain leading into a cycle: D -> A -> B -> C -> A.

        Only the cycle {A, B, C} is reported, not D.
        """
        graph = {
            "d": {"a"},
            "a": {"b"},
            "b": {"c"},
            "c": {"a"},
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["a", "b", "c"]

    def test_large_scc(self):
        """A fully connected 4-node graph is one big SCC."""
        nodes = ["a", "b", "c", "d"]
        graph = {n: set(nodes) - {n} for n in nodes}
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert sorted(cycles[0]) == sorted(nodes)

    def test_nodes_only_in_targets_included(self):
        """Nodes that appear only as targets (not keys) are considered."""
        graph = {
            "a": {"b"},
            "b": {"c"},
            "c": {"b"},  # b <-> c cycle, 'c' only appears as a target of 'b'
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["b", "c"]

    def test_results_are_sorted(self):
        """Each cycle's file list is sorted for deterministic output."""
        graph = {
            "z": {"a"},
            "a": {"z"},
        }
        cycles = find_circular_deps(graph)
        assert len(cycles) == 1
        assert cycles[0] == ["a", "z"]


# ---------------------------------------------------------------------------
# Python circular dependency detection
# ---------------------------------------------------------------------------


_PYPROJECT = '[project]\nname = "example"\n'


class TestFindCircularPythonDeps:
    """find_circular_python_deps detects cycles in Python projects."""

    def test_no_cycles_clean_project(self, tmp_path):
        """A Python project with no circular imports returns empty."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("from .utils import helper\n")
        (pkg / "utils.py").write_text("def helper(): pass\n")

        cycles = find_circular_python_deps(str(tmp_path))
        assert cycles == []

    def test_two_file_cycle_detected(self, tmp_path):
        """A -> B -> A cycle in Python is detected with warn severity."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from .b import y\nx = 1\n")
        (pkg / "b.py").write_text("from .a import x\ny = 2\n")

        cycles = find_circular_python_deps(str(tmp_path))
        assert len(cycles) == 1
        # Cycle contains both files (relative paths)
        cycle_files = set(cycles[0])
        assert "mylib/a.py" in cycle_files
        assert "mylib/b.py" in cycle_files

    def test_three_file_cycle_detected(self, tmp_path):
        """A -> B -> C -> A cycle is detected."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from .b import y\nx = 1\n")
        (pkg / "b.py").write_text("from .c import z\ny = 2\n")
        (pkg / "c.py").write_text("from .a import x\nz = 3\n")

        cycles = find_circular_python_deps(str(tmp_path))
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["mylib/a.py", "mylib/b.py", "mylib/c.py"]

    def test_non_python_project_returns_empty(self, tmp_path):
        """No pyproject.toml means no Python analysis."""
        (tmp_path / "main.go").write_text("package main\n")
        cycles = find_circular_python_deps(str(tmp_path))
        assert cycles == []

    def test_test_files_excluded(self, tmp_path):
        """Cycles in test files are not reported."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        # Test directory with a cycle -- should not be detected
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("from tests.test_b import y\nx = 1\n")
        (tests / "test_b.py").write_text("from tests.test_a import x\ny = 2\n")

        cycles = find_circular_python_deps(str(tmp_path))
        assert cycles == []


# ---------------------------------------------------------------------------
# npm circular dependency detection
# ---------------------------------------------------------------------------


class TestFindCircularNpmDeps:
    """find_circular_npm_deps detects cycles in npm projects."""

    def _pkg_json(self, path, data):
        """Write a package.json with the given dict data."""
        (path / "package.json").write_text(json.dumps(data))

    def test_no_cycles_clean_project(self, tmp_path):
        """An npm project with no circular imports returns empty."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("const util = require('./util');\n")
        (src / "util.js").write_text("module.exports = {};\n")

        cycles = find_circular_npm_deps(str(tmp_path))
        assert cycles == []

    def test_two_file_cycle_detected(self, tmp_path):
        """A -> B -> A cycle in npm is detected (error severity in check)."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.js").write_text("const b = require('./b');\nmodule.exports = {};\n")
        (src / "b.js").write_text("const a = require('./a');\nmodule.exports = {};\n")

        cycles = find_circular_npm_deps(str(tmp_path))
        assert len(cycles) == 1
        cycle_files = set(cycles[0])
        assert "src/a.js" in cycle_files
        assert "src/b.js" in cycle_files

    def test_three_file_cycle_detected(self, tmp_path):
        """A -> B -> C -> A cycle in npm is detected."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.js").write_text("const b = require('./b');\n")
        (src / "b.js").write_text("const c = require('./c');\n")
        (src / "c.js").write_text("const a = require('./a');\n")

        cycles = find_circular_npm_deps(str(tmp_path))
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["src/a.js", "src/b.js", "src/c.js"]

    def test_no_package_json_returns_empty(self, tmp_path):
        """Directory without package.json returns empty list."""
        (tmp_path / "index.js").write_text("module.exports = {};\n")
        cycles = find_circular_npm_deps(str(tmp_path))
        assert cycles == []

    def test_typescript_cycle_detected(self, tmp_path):
        """Cycles in TypeScript files are detected."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.ts",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.ts").write_text("import { y } from './b';\nexport const x = 1;\n")
        (src / "b.ts").write_text("import { x } from './a';\nexport const y = 2;\n")

        cycles = find_circular_npm_deps(str(tmp_path))
        assert len(cycles) == 1
        cycle_files = set(cycles[0])
        assert "src/a.ts" in cycle_files
        assert "src/b.ts" in cycle_files


# ---------------------------------------------------------------------------
# Dart circular dependency detection
# ---------------------------------------------------------------------------


class TestFindCircularDartDeps:
    """find_circular_dart_deps detects cycles in Dart projects."""

    def test_no_pubspec_returns_empty(self, tmp_path):
        """No pubspec.yaml means no Dart analysis."""
        (tmp_path / "main.dart").write_text("void main() {}\n")
        cycles = find_circular_dart_deps(str(tmp_path))
        assert cycles == []

    def test_no_cycles_clean_project(self, tmp_path):
        """A Dart project with no circular imports returns empty."""
        (tmp_path / "pubspec.yaml").write_text("name: example\n")
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "a.dart").write_text("import 'b.dart';\n")
        (lib / "b.dart").write_text("void hello() {}\n")

        cycles = find_circular_dart_deps(str(tmp_path))
        assert cycles == []

    def test_two_file_cycle_detected(self, tmp_path):
        """A -> B -> A cycle in Dart is detected."""
        (tmp_path / "pubspec.yaml").write_text("name: example\n")
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "a.dart").write_text("import 'b.dart';\nvoid a() {}\n")
        (lib / "b.dart").write_text("import 'a.dart';\nvoid b() {}\n")

        cycles = find_circular_dart_deps(str(tmp_path))
        assert len(cycles) == 1
        cycle_files = set(cycles[0])
        assert "lib/a.dart" in cycle_files
        assert "lib/b.dart" in cycle_files

    def test_package_imports_ignored(self, tmp_path):
        """Package imports (package:foo/...) don't contribute to cycles."""
        (tmp_path / "pubspec.yaml").write_text("name: example\n")
        lib = tmp_path / "lib"
        lib.mkdir()
        # Only package imports, no relative imports -- no cycle possible
        (lib / "a.dart").write_text("import 'package:other/other.dart';\n")
        (lib / "b.dart").write_text("import 'dart:core';\n")

        cycles = find_circular_dart_deps(str(tmp_path))
        assert cycles == []


# ---------------------------------------------------------------------------
# Check registration integration tests
# ---------------------------------------------------------------------------


class TestCircularDepsCheck:
    """Integration tests: circular-deps check registered on the strictcli system."""

    def _capture_checks(self):
        """Register all checks on a mock app and return the captured dict."""
        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())
        return captured

    def test_registered(self):
        """circular-deps check is registered."""
        captured = self._capture_checks()
        assert "circular-deps" in captured

    def test_skip_unsupported_target(self, tmp_path):
        """circular-deps skips for projects that are neither Python, npm, nor Dart."""
        from rlsbl.context import ProjectContext

        # Create a Go-only project
        (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["circular-deps"](ctx)
        assert result.status == "skip"

    def test_pass_no_cycles_python(self, tmp_path):
        """circular-deps passes for a Python project with no cycles."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import run\n")
        (pkg / "core.py").write_text("def run(): pass\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["circular-deps"](ctx)
        assert result.status == "pass"

    def test_warn_python_cycle(self, tmp_path):
        """circular-deps warns for a Python project with a cycle (Python = warn)."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from .b import y\nx = 1\n")
        (pkg / "b.py").write_text("from .a import x\ny = 2\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["circular-deps"](ctx)
        assert result.status == "warn"
        assert "circular dependency cycle" in result.message

    def test_fail_npm_cycle(self, tmp_path):
        """circular-deps fails for an npm project with a cycle (npm = error)."""
        from rlsbl.context import ProjectContext

        (tmp_path / "package.json").write_text(json.dumps({
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        }))
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.js").write_text("const b = require('./b');\nmodule.exports = {};\n")
        (src / "b.js").write_text("const a = require('./a');\nmodule.exports = {};\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["circular-deps"](ctx)
        assert result.status == "fail"
        assert "circular dependency cycle" in result.message

    def test_details_show_cycle_path(self, tmp_path):
        """Details show the cycle as a chain of files."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from .b import y\nx = 1\n")
        (pkg / "b.py").write_text("from .a import x\ny = 2\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["circular-deps"](ctx)
        assert result.details is not None
        assert len(result.details) >= 1
        # Detail should contain " -> " showing the cycle
        assert " -> " in result.details[0]
