"""Tests for the namespace package import fix (phases 5-7).

Covers:
- build_namespace_map with orxt-style layout
- build_namespace_map with no namespace (flat package)
- ImportRecord dataclass fields
- Composite matching: top-level wins, import_name overrides, namespace map fallback
- Sub-component matching for namespace imports
- End-to-end: workspace check with namespace imports
"""

import os

import pytest

from rlsbl.import_scanners import (
    ImportInfo,
    PythonImportScanner,
    build_namespace_map,
)
from rlsbl.lint.python_ast import ImportRecord
from rlsbl.targets.utils import detect_python_package_root


class TestDetectPythonPackageRoot:
    """detect_python_package_root returns the package root from hatch config or filesystem."""

    def test_hatch_src_layout(self, tmp_path):
        """Hatch config with src/orxt returns 'src/orxt'."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "protocols"\n'
            '[tool.hatch.build.targets.wheel]\n'
            'packages = ["src/orxt"]\n'
        )
        result = detect_python_package_root(str(tmp_path))
        assert result == "src/orxt"

    def test_filesystem_underscored(self, tmp_path):
        """Falls back to underscored directory name when no hatch config."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-lib"\n'
        )
        (tmp_path / "my_lib").mkdir()
        result = detect_python_package_root(str(tmp_path))
        assert result == "my_lib"

    def test_filesystem_raw_name(self, tmp_path):
        """Falls back to raw project name directory when underscored not found."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\n'
        )
        (tmp_path / "mylib").mkdir()
        result = detect_python_package_root(str(tmp_path))
        assert result == "mylib"

    def test_convention_fallback(self, tmp_path):
        """Falls back to underscored convention when no directory exists."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-lib"\n'
        )
        result = detect_python_package_root(str(tmp_path))
        assert result == "my_lib"

    def test_no_pyproject(self, tmp_path):
        """Returns None when pyproject.toml is missing."""
        result = detect_python_package_root(str(tmp_path))
        assert result is None

    def test_no_project_name(self, tmp_path):
        """Returns None when project name is empty."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        result = detect_python_package_root(str(tmp_path))
        assert result is None


class TestBuildNamespaceMap:
    """build_namespace_map maps namespace-qualified paths to project names."""

    def test_orxt_style_layout(self, tmp_path):
        """Project 'protocols' at protocols/src/orxt/protocols/ maps orxt.protocols."""
        ws_root = str(tmp_path)

        # Create protocols project with orxt namespace
        proto_dir = tmp_path / "protocols"
        proto_dir.mkdir()
        (proto_dir / "pyproject.toml").write_text(
            '[project]\nname = "protocols"\n'
            '[tool.hatch.build.targets.wheel]\n'
            'packages = ["src/orxt"]\n'
        )
        pkg_root = proto_dir / "src" / "orxt" / "protocols"
        pkg_root.mkdir(parents=True)

        projects = [{"name": "protocols", "path": "protocols"}]
        result = build_namespace_map(projects, ws_root)
        assert result == {"orxt.protocols": "protocols"}

    def test_multiple_packages_same_namespace(self, tmp_path):
        """Multiple workspace packages under the same namespace are all mapped."""
        ws_root = str(tmp_path)

        for name in ("protocols", "transport"):
            proj_dir = tmp_path / name
            proj_dir.mkdir()
            (proj_dir / "pyproject.toml").write_text(
                f'[project]\nname = "{name}"\n'
                '[tool.hatch.build.targets.wheel]\n'
                'packages = ["src/orxt"]\n'
            )
            (proj_dir / "src" / "orxt" / name).mkdir(parents=True)

        projects = [
            {"name": "protocols", "path": "protocols"},
            {"name": "transport", "path": "transport"},
        ]
        result = build_namespace_map(projects, ws_root)
        assert result == {
            "orxt.protocols": "protocols",
            "orxt.transport": "transport",
        }

    def test_flat_package_no_namespace(self, tmp_path):
        """Project with flat layout (no namespace) produces no namespace map entries."""
        ws_root = str(tmp_path)

        proj_dir = tmp_path / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\n'
        )
        (proj_dir / "mylib").mkdir()

        projects = [{"name": "mylib", "path": "mylib"}]
        result = build_namespace_map(projects, ws_root)
        # Flat package: the package root IS the project name, so
        # namespace == project name, and no subdirectory named after
        # the project exists inside the package root itself (it IS the root).
        assert result == {}

    def test_no_pyproject(self, tmp_path):
        """Project without pyproject.toml is skipped."""
        ws_root = str(tmp_path)
        proj_dir = tmp_path / "noproject"
        proj_dir.mkdir()

        projects = [{"name": "noproject", "path": "noproject"}]
        result = build_namespace_map(projects, ws_root)
        assert result == {}

    def test_hyphenated_project_name(self, tmp_path):
        """Project with hyphens in name matches underscored directory."""
        ws_root = str(tmp_path)

        proj_dir = tmp_path / "my-protocols"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "my-protocols"\n'
            '[tool.hatch.build.targets.wheel]\n'
            'packages = ["src/orxt"]\n'
        )
        (proj_dir / "src" / "orxt" / "my_protocols").mkdir(parents=True)

        projects = [{"name": "my-protocols", "path": "my-protocols"}]
        result = build_namespace_map(projects, ws_root)
        assert result == {"orxt.my_protocols": "my-protocols"}


class TestImportRecord:
    """ImportRecord dataclass has the expected fields."""

    def test_fields(self):
        """ImportRecord has top_level, full_path, filepath, line, guarded, type_checking."""
        record = ImportRecord(
            top_level="orxt",
            full_path="orxt.protocols",
            filepath="/a/b.py",
            line=5,
            guarded=False,
        )
        assert record.top_level == "orxt"
        assert record.full_path == "orxt.protocols"
        assert record.filepath == "/a/b.py"
        assert record.line == 5
        assert record.guarded is False
        assert record.type_checking is False

    def test_type_checking_field(self):
        """ImportRecord type_checking field defaults to False and can be set."""
        record_default = ImportRecord(
            top_level="orxt", full_path="orxt.protocols",
            filepath="/a.py", line=1,
        )
        assert record_default.type_checking is False

        record_tc = ImportRecord(
            top_level="orxt", full_path="orxt.protocols",
            filepath="/a.py", line=1, type_checking=True,
        )
        assert record_tc.type_checking is True

    def test_frozen(self):
        """ImportRecord is frozen (immutable)."""
        record = ImportRecord(
            top_level="orxt", full_path="orxt.protocols",
            filepath="/a.py", line=1,
        )
        with pytest.raises(AttributeError):
            record.top_level = "changed"


class TestCompositeMatching:
    """PythonImportScanner composite matching order."""

    def test_top_level_match_wins(self, tmp_path):
        """Direct top-level match takes priority over namespace matching."""
        (tmp_path / "app.py").write_text("import protocols\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
            namespace_map={"orxt.protocols": "protocols"},
        )
        assert len(results) == 1
        assert results[0].package_name == "protocols"

    def test_import_name_override(self, tmp_path):
        """import_name override matches when top-level doesn't."""
        (tmp_path / "app.py").write_text("import custom_name\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"myproject"},
            import_names={"myproject": "custom_name"},
        )
        assert len(results) == 1
        assert results[0].package_name == "myproject"

    def test_import_name_prefix_match(self, tmp_path):
        """import_name matches when full_path starts with import_name + '.'."""
        (tmp_path / "app.py").write_text("from custom_name.sub import Thing\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"myproject"},
            import_names={"myproject": "custom_name"},
        )
        assert len(results) == 1
        assert results[0].package_name == "myproject"

    def test_namespace_map_fallback(self, tmp_path):
        """Namespace map matches when top-level and import_name don't."""
        (tmp_path / "app.py").write_text("from orxt.protocols import Tool\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
            namespace_map={"orxt.protocols": "protocols"},
        )
        assert len(results) == 1
        assert results[0].package_name == "protocols"

    def test_namespace_map_longest_prefix(self, tmp_path):
        """Longest namespace prefix wins when multiple could match."""
        (tmp_path / "app.py").write_text("from orxt.protocols.grpc import Channel\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols", "grpc-proto"},
            namespace_map={
                "orxt.protocols": "protocols",
                "orxt.protocols.grpc": "grpc-proto",
            },
        )
        # orxt.protocols.grpc is longer, should match grpc-proto
        assert len(results) == 1
        assert results[0].package_name == "grpc-proto"

    def test_sub_component_match(self, tmp_path):
        """Sub-component matching finds workspace name in dotted path."""
        (tmp_path / "app.py").write_text("from orxt.protocols import Tool\n")
        scanner = PythonImportScanner()
        # No namespace_map, no import_names -- relies on sub-component matching
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
        )
        assert len(results) == 1
        assert results[0].package_name == "protocols"

    def test_no_false_positive_stdlib_subcomponent(self, tmp_path):
        """Stdlib modules are excluded before sub-component matching."""
        (tmp_path / "app.py").write_text("import os.path\n")
        scanner = PythonImportScanner()
        # 'os' is stdlib, 'path' could theoretically match
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"path"},
        )
        # os is stdlib, so entire import is skipped
        assert results == []

    def test_no_match_returns_empty(self, tmp_path):
        """Import with no workspace match returns empty results."""
        (tmp_path / "app.py").write_text("import requests\n")
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
        )
        assert results == []


class TestEndToEnd:
    """End-to-end: namespace imports detected through the full scanner pipeline."""

    def test_workspace_namespace_imports(self, tmp_path):
        """Full pipeline: multiple namespace imports from different workspace packages."""
        (tmp_path / "app.py").write_text(
            "from orxt.protocols import Tool\n"
            "from orxt.transport import Bus\n"
            "import json\n"
            "import regular_lib\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols", "transport", "regular_lib"},
            namespace_map={
                "orxt.protocols": "protocols",
                "orxt.transport": "transport",
            },
        )
        names = {r.package_name for r in results}
        assert names == {"protocols", "transport", "regular_lib"}

    def test_test_context_preserved(self, tmp_path):
        """Namespace imports in test files are marked as test context."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text(
            "from orxt.protocols import Tool\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
            namespace_map={"orxt.protocols": "protocols"},
        )
        assert len(results) == 1
        assert results[0].is_test_context is True
        assert results[0].package_name == "protocols"

    def test_guarded_import_preserved(self, tmp_path):
        """Namespace imports inside try/except ImportError are marked guarded."""
        (tmp_path / "app.py").write_text(
            "try:\n"
            "    from orxt.protocols import Tool\n"
            "except ImportError:\n"
            "    Tool = None\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path),
            workspace_names={"protocols"},
            namespace_map={"orxt.protocols": "protocols"},
        )
        assert len(results) == 1
        assert results[0].guarded is True
        assert results[0].package_name == "protocols"
