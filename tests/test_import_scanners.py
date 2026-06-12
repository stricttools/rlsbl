"""Tests for rlsbl.import_scanners -- Python, Dart, npm, and Go import scanners."""

import os

import pytest

from rlsbl.import_scanners import (
    DartImportScanner,
    GoImportScanner,
    ImportInfo,
    NpmImportScanner,
    PythonImportScanner,
    _is_test_context,
)

# Minimal pyproject.toml so language detection finds Python
_PYPROJECT = '[project]\nname = "example"\n'


class TestPythonImportScanner:
    """PythonImportScanner filters to workspace-relevant imports."""

    def test_only_workspace_imports_returned(self, tmp_path):
        """Importing 3 packages (2 workspace, 1 external) returns only 2."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text(
            "import alpha\nimport beta\nimport requests\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"alpha", "beta"})
        names = {r.package_name for r in results}
        assert names == {"alpha", "beta"}

    def test_stdlib_excluded(self, tmp_path):
        """Stdlib imports (os, sys, pathlib) are excluded."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text(
            "import os\nimport sys\nimport pathlib\nimport mylib\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"os", "sys", "pathlib", "mylib"})
        # os, sys, pathlib are stdlib -- only mylib should pass
        names = {r.package_name for r in results}
        assert names == {"mylib"}

    def test_relative_imports_excluded(self, tmp_path):
        """Relative imports (from . import X) are excluded."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "mod.py").write_text(
            "from . import sibling\nfrom ..parent import stuff\nimport alpha\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"alpha", "sibling", "parent"})
        names = {r.package_name for r in results}
        assert names == {"alpha"}

    def test_test_context_detection(self, tmp_path):
        """Files in test/ or tests/ have is_test_context=True; others False."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)

        # Source file
        (tmp_path / "src.py").write_text("import alpha\n")

        # Test file in tests/
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("import alpha\n")

        # Test file in test/
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "test_bar.py").write_text("import alpha\n")

        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"alpha"})

        by_file = {os.path.basename(r.file_path): r.is_test_context for r in results}
        assert by_file["src.py"] is False
        assert by_file["test_foo.py"] is True
        assert by_file["test_bar.py"] is True

    def test_pypi_normalization(self, tmp_path):
        """Import names are normalized via PEP 503 (e.g. my_lib -> my-lib)."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import my_lib\n")
        scanner = PythonImportScanner()
        # Workspace has hyphenated name; import uses underscore
        results = scanner.scan(str(tmp_path), {"my-lib"})
        assert len(results) == 1
        assert results[0].package_name == "my-lib"

    def test_empty_project_no_results(self, tmp_path):
        """A project with no Python files returns empty list."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"alpha"})
        assert results == []

    def test_no_language_marker_still_scans(self, tmp_path):
        """Without pyproject.toml, scanner still finds .py files.

        The import scanner walks for .py files directly (via the AST
        linter), independent of language detection markers.
        """
        (tmp_path / "lib.py").write_text("import alpha\n")
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), {"alpha"})
        assert len(results) == 1
        assert results[0].package_name == "alpha"


class TestDartImportScanner:
    """DartImportScanner detects package imports in .dart files."""

    def test_package_import_detected(self, tmp_path):
        """import 'package:models/model.dart' detects 'models'."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1
        assert results[0].package_name == "models"
        assert results[0].line_number == 1
        assert results[0].is_test_context is False

    def test_dart_sdk_import_excluded(self, tmp_path):
        """import 'dart:core' is excluded (SDK import, not a package)."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'dart:core';\nimport 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models", "core"})
        names = {r.package_name for r in results}
        # dart:core is SDK, not package -- only models matches
        assert names == {"models"}

    def test_relative_import_excluded(self, tmp_path):
        """Relative imports (no package: prefix) are excluded."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'utils.dart';\nimport '../other.dart';\nimport 'package:foo/bar.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"foo", "utils", "other"})
        names = {r.package_name for r in results}
        assert names == {"foo"}

    def test_export_statement_detected(self, tmp_path):
        """export 'package:foo/bar.dart' detects 'foo'."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "export 'package:foo/bar.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"foo"})
        assert len(results) == 1
        assert results[0].package_name == "foo"

    def test_test_context_true_for_test_dir(self, tmp_path):
        """Files in test/ directory have is_test_context=True."""
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "widget_test.dart").write_text(
            "import 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1
        assert results[0].is_test_context is True

    def test_lib_context_is_not_test(self, tmp_path):
        """Files in lib/ directory have is_test_context=False."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "widget.dart").write_text(
            "import 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1
        assert results[0].is_test_context is False

    def test_non_workspace_import_excluded(self, tmp_path):
        """Imports of packages not in workspace_names are excluded."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'package:http/http.dart';\n"
            "import 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1
        assert results[0].package_name == "models"

    def test_generated_file_check_error(self, tmp_path):
        """build.yaml exists but no .g.dart files raises RuntimeError."""
        (tmp_path / "build.yaml").write_text("targets:\n  $default:\n")
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'package:models/model.dart';\n"
        )
        scanner = DartImportScanner()
        with pytest.raises(RuntimeError, match="no .g.dart files found"):
            scanner.scan(str(tmp_path), {"models"})

    def test_generated_file_check_passes_with_g_dart(self, tmp_path):
        """build.yaml exists with .g.dart files does not raise."""
        (tmp_path / "build.yaml").write_text("targets:\n  $default:\n")
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'package:models/model.dart';\n"
        )
        (lib_dir / "main.g.dart").write_text("// generated\n")
        scanner = DartImportScanner()
        # Should not raise
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1

    def test_no_build_yaml_no_check(self, tmp_path):
        """Without build.yaml, no generated file check is performed."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            "import 'package:foo/bar.dart';\n"
        )
        scanner = DartImportScanner()
        # Should not raise even with no .g.dart files
        results = scanner.scan(str(tmp_path), {"foo"})
        assert len(results) == 1

    def test_double_quoted_imports(self, tmp_path):
        """Dart imports with double quotes are also detected."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "main.dart").write_text(
            'import "package:models/model.dart";\n'
        )
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert len(results) == 1
        assert results[0].package_name == "models"

    def test_empty_project_returns_empty(self, tmp_path):
        """A project with no .dart files returns an empty list."""
        scanner = DartImportScanner()
        results = scanner.scan(str(tmp_path), {"models"})
        assert results == []


class TestNpmImportScanner:
    """NpmImportScanner detects JS/TS imports from workspace packages."""

    def test_finds_workspace_import(self, tmp_path):
        """A JS file importing a workspace package is detected."""
        (tmp_path / "index.js").write_text(
            "const auth = require('auth');\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert len(results) == 1
        assert results[0].package_name == "auth"
        assert results[0].is_test_context is False

    def test_ignores_relative(self, tmp_path):
        """Relative imports (./foo, ../bar) are excluded."""
        (tmp_path / "index.js").write_text(
            "import './foo';\n"
            "import '../bar';\n"
            "import '/absolute/path';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"foo", "bar"})
        assert results == []

    def test_ignores_builtin(self, tmp_path):
        """Node.js built-in modules (fs, path, crypto, etc.) are excluded."""
        (tmp_path / "index.ts").write_text(
            "import fs from 'fs';\n"
            "import path from 'path';\n"
            "import crypto from 'crypto';\n"
            "import http from 'http';\n"
            "import os from 'os';\n"
            "import { spawn } from 'child_process';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(
            str(tmp_path), {"fs", "path", "crypto", "http", "os", "child_process"}
        )
        assert results == []

    def test_ignores_node_prefixed_builtin(self, tmp_path):
        """node:-prefixed builtins (node:fs, node:path) are excluded."""
        (tmp_path / "index.ts").write_text(
            "import fs from 'node:fs';\n"
            "import { join } from 'node:path';\n"
            "import { readFile } from 'node:fs/promises';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"fs", "path"})
        assert results == []

    def test_scoped_package(self, tmp_path):
        """Scoped package @scope/pkg is detected when in workspace_names."""
        (tmp_path / "index.ts").write_text(
            "import { thing } from '@myorg/utils';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"@myorg/utils"})
        assert len(results) == 1
        assert results[0].package_name == "@myorg/utils"

    def test_scoped_package_with_subpath(self, tmp_path):
        """@scope/pkg/subpath extracts @scope/pkg as the bare name."""
        (tmp_path / "index.ts").write_text(
            "import { helper } from '@myorg/utils/helpers';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"@myorg/utils"})
        assert len(results) == 1
        assert results[0].package_name == "@myorg/utils"

    def test_test_context(self, tmp_path):
        """Import in a tests/ directory has is_test_context=True."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.js").write_text(
            "const auth = require('auth');\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert len(results) == 1
        assert results[0].is_test_context is True

    def test_bare_name_extraction(self, tmp_path):
        """import 'pkg/subpath' extracts bare name 'pkg'."""
        (tmp_path / "index.js").write_text(
            "import 'mylib/utils/helpers';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"mylib"})
        assert len(results) == 1
        assert results[0].package_name == "mylib"

    def test_case_insensitive_matching(self, tmp_path):
        """npm names are case-insensitive: MyLib matches mylib."""
        (tmp_path / "index.js").write_text(
            "import MyLib from 'MyLib';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"mylib"})
        assert len(results) == 1
        assert results[0].package_name == "mylib"

    def test_non_workspace_import_excluded(self, tmp_path):
        """Imports of packages not in workspace_names are excluded."""
        (tmp_path / "index.js").write_text(
            "import express from 'express';\n"
            "import auth from 'auth';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert len(results) == 1
        assert results[0].package_name == "auth"

    def test_empty_project_returns_empty(self, tmp_path):
        """A project with no JS/TS files returns an empty list."""
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert results == []

    def test_typescript_file(self, tmp_path):
        """TypeScript (.ts) files are scanned."""
        (tmp_path / "app.ts").write_text(
            "import { Service } from 'auth';\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert len(results) == 1
        assert results[0].package_name == "auth"

    def test_dynamic_import(self, tmp_path):
        """Dynamic import() calls are detected."""
        (tmp_path / "index.js").write_text(
            "const auth = await import('auth');\n"
        )
        scanner = NpmImportScanner()
        results = scanner.scan(str(tmp_path), {"auth"})
        assert len(results) == 1
        assert results[0].package_name == "auth"


class TestGoImportScanner:
    """GoImportScanner detects Go imports from workspace sibling modules."""

    def _write_go_mod(self, path, module_path):
        """Write a go.mod with the given module path."""
        (path / "go.mod").write_text(f"module {module_path}\n\ngo 1.21\n")

    def test_workspace_sibling_import_detected(self, tmp_path):
        """A Go file importing a workspace sibling's module path is detected."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB/pkg"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "projB"
        assert results[0].is_test_context is False

    def test_exact_module_path_match(self, tmp_path):
        """Importing the exact module path (no subpackage) is detected."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "projB"

    def test_external_import_not_detected(self, tmp_path):
        """Importing an external package not in the workspace is not detected."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/external/lib"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert results == []

    def test_self_import_excluded(self, tmp_path):
        """Importing the project's own module path is excluded."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projA/internal/util"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert results == []

    def test_test_file_context_detection(self, tmp_path):
        """Files ending in _test.go have is_test_context=True."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB"\n'
        )
        (tmp_path / "main_test.go").write_text(
            'package main\n\nimport "github.com/org/projB"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        by_file = {
            os.path.basename(r.file_path): r.is_test_context for r in results
        }
        assert by_file["main.go"] is False
        assert by_file["main_test.go"] is True

    def test_test_directory_context_detection(self, tmp_path):
        """Files in a tests/ directory have is_test_context=True."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "integration.go").write_text(
            'package tests\n\nimport "github.com/org/projB"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert len(results) == 1
        assert results[0].is_test_context is True

    def test_no_module_path_map_returns_empty(self, tmp_path):
        """Without module_path_map, GoImportScanner returns empty results."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB"\n'
        )
        scanner = GoImportScanner()
        results = scanner.scan(str(tmp_path), {"projA", "projB"})
        assert results == []

    def test_no_go_mod_returns_empty(self, tmp_path):
        """A project without go.mod returns empty results."""
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projB": "github.com/org/projB",
        }
        # No go.mod means own_module_path is None; all sibling modules
        # are still checked (since None != any module path).
        results = scanner.scan(
            str(tmp_path), {"projB"},
            module_path_map=module_path_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "projB"

    def test_grouped_imports(self, tmp_path):
        """Grouped import statements are all detected."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport (\n'
            '\t"fmt"\n'
            '\t"github.com/org/projB"\n'
            '\t"github.com/org/projC/internal/util"\n'
            ')\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
            "projC": "github.com/org/projC",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB", "projC"},
            module_path_map=module_path_map,
        )
        names = {r.package_name for r in results}
        assert names == {"projB", "projC"}

    def test_empty_project_returns_empty(self, tmp_path):
        """A project with no Go files returns an empty list."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert results == []

    def test_stdlib_import_not_detected(self, tmp_path):
        """Standard library imports (fmt, os, etc.) are not detected."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        (tmp_path / "main.go").write_text(
            'package main\n\nimport (\n\t"fmt"\n\t"os"\n)\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
        }
        results = scanner.scan(
            str(tmp_path), {"projA"},
            module_path_map=module_path_map,
        )
        assert results == []

    def test_partial_module_path_no_false_positive(self, tmp_path):
        """A module path that is a prefix but not followed by '/' is not matched."""
        self._write_go_mod(tmp_path, "github.com/org/projA")
        # projB-extra should NOT match projB
        (tmp_path / "main.go").write_text(
            'package main\n\nimport "github.com/org/projB-extra"\n'
        )
        scanner = GoImportScanner()
        module_path_map = {
            "projA": "github.com/org/projA",
            "projB": "github.com/org/projB",
        }
        results = scanner.scan(
            str(tmp_path), {"projA", "projB"},
            module_path_map=module_path_map,
        )
        assert results == []


class TestImportInfo:
    """ImportInfo dataclass behavior."""

    def test_frozen(self):
        """ImportInfo is immutable (frozen dataclass)."""
        info = ImportInfo(
            package_name="foo",
            file_path="/a/b.py",
            line_number=1,
            is_test_context=False,
        )
        with pytest.raises(AttributeError):
            info.package_name = "bar"

    def test_equality(self):
        """Two ImportInfo with same fields are equal."""
        a = ImportInfo("foo", "/a.py", 1, False)
        b = ImportInfo("foo", "/a.py", 1, False)
        assert a == b


class TestIsTestContext:
    """_is_test_context detection for directories and file name patterns.

    Tests the layered matching approach:
    - __tests__/ and testdata/ match at any depth in the path
    - test/, tests/, example/, examples/, integration_test/ match only
      as the first path component (root-relative)
    - File name patterns (conftest.py, *.test.js, etc.) always match
    """

    # -- Directory detection: anywhere-match patterns --

    def test_dunder_tests_dir_at_root(self, tmp_path):
        """__tests__/Component.test.js -- test context (anywhere match)."""
        filepath = os.path.join(tmp_path, "__tests__", "Component.test.js")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_dunder_tests_dir_deeply_nested(self, tmp_path):
        """deep/nested/__tests__/foo.test.js -- test context (__tests__ anywhere)."""
        filepath = os.path.join(
            tmp_path, "deep", "nested", "__tests__", "foo.test.js"
        )
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_testdata_dir_at_root(self, tmp_path):
        """testdata/fixtures.json -- test context (anywhere match)."""
        filepath = os.path.join(tmp_path, "testdata", "fixtures.json")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_testdata_dir_deeply_nested(self, tmp_path):
        """deep/nested/testdata/fixture.go -- test context (testdata anywhere)."""
        filepath = os.path.join(
            tmp_path, "deep", "nested", "testdata", "fixture.go"
        )
        assert _is_test_context(filepath, str(tmp_path)) is True

    # -- Directory detection: root-relative-only patterns --

    def test_examples_dir_at_root(self, tmp_path):
        """examples/demo.py -- test context (root-relative)."""
        filepath = os.path.join(tmp_path, "examples", "demo.py")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_example_dir_at_root(self, tmp_path):
        """example/main.dart -- test context (root-relative)."""
        filepath = os.path.join(tmp_path, "example", "main.dart")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_integration_test_dir_at_root(self, tmp_path):
        """integration_test/widget_test.dart -- test context (root-relative, Dart)."""
        filepath = os.path.join(
            tmp_path, "integration_test", "widget_test.dart"
        )
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_src_test_is_not_test_context(self, tmp_path):
        """src/test/utils.py -- NOT test context (false positive fix).

        test/ only matches as the first component, not nested under src/.
        """
        filepath = os.path.join(tmp_path, "src", "test", "utils.py")
        assert _is_test_context(filepath, str(tmp_path)) is False

    def test_src_examples_is_not_test_context(self, tmp_path):
        """src/examples/demo.py -- NOT test context (not first component)."""
        filepath = os.path.join(tmp_path, "src", "examples", "demo.py")
        assert _is_test_context(filepath, str(tmp_path)) is False

    def test_src_test_production_code(self, tmp_path):
        """src/test/production_code.py -- NOT test context (key false positive fix)."""
        filepath = os.path.join(
            tmp_path, "src", "test", "production_code.py"
        )
        assert _is_test_context(filepath, str(tmp_path)) is False

    # -- File name pattern tests --

    def test_conftest_py_at_root(self, tmp_path):
        """conftest.py at project root -- test context."""
        filepath = os.path.join(tmp_path, "conftest.py")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_spec_tsx_file(self, tmp_path):
        """Component.spec.tsx -- test context."""
        filepath = os.path.join(tmp_path, "src", "Component.spec.tsx")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_dot_test_js_file(self, tmp_path):
        """utils.test.js -- test context."""
        filepath = os.path.join(tmp_path, "src", "utils.test.js")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_underscore_test_py_file(self, tmp_path):
        """foo_test.py -- test context."""
        filepath = os.path.join(tmp_path, "foo_test.py")
        assert _is_test_context(filepath, str(tmp_path)) is True

    def test_widget_test_dart_file(self, tmp_path):
        """widget_test.dart -- test context."""
        filepath = os.path.join(tmp_path, "widget_test.dart")
        assert _is_test_context(filepath, str(tmp_path)) is True

    # -- Negative tests (should NOT be test context) --

    def test_testing_dir_not_matched(self, tmp_path):
        """testing/foo.py -- NOT test context ("testing" is not in pattern list)."""
        filepath = os.path.join(tmp_path, "testing", "foo.py")
        assert _is_test_context(filepath, str(tmp_path)) is False

    def test_tested_dir_not_matched(self, tmp_path):
        """tested/module.py -- NOT test context ("tested" is not in pattern list)."""
        filepath = os.path.join(tmp_path, "tested", "module.py")
        assert _is_test_context(filepath, str(tmp_path)) is False

    def test_contest_py_not_matched(self, tmp_path):
        """contest.py -- NOT test context (should not match conftest.py)."""
        filepath = os.path.join(tmp_path, "contest.py")
        assert _is_test_context(filepath, str(tmp_path)) is False
