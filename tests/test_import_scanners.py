"""Tests for rlsbl.import_scanners -- Python, Dart, and npm import scanners."""

import os

import pytest

from rlsbl.import_scanners import (
    DartImportScanner,
    ImportInfo,
    NpmImportScanner,
    PythonImportScanner,
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
