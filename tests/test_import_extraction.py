"""Tests for import extraction from lint AST parsers."""

import json
import os

import pytest

from rlsbl.lint import scan_imports
from rlsbl.lint.npm_ast import NpmAstLinter
from rlsbl.lint.protocol import ImportScanner
from rlsbl.lint.python_ast import PythonAstLinter


_PYPROJECT = '[project]\nname = "example"\n'


class TestPythonImportExtraction:
    """Extract imports from Python source files using tree-sitter AST."""

    def test_basic_imports(self, tmp_path):
        """import os, from pathlib import Path, import requests all collected."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text(
            "import os\nfrom pathlib import Path\nimport requests\n"
        )
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"os", "pathlib", "requests"}

    def test_line_numbers(self, tmp_path):
        """Each import records the correct 1-based line number."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text(
            "import os\n\nfrom pathlib import Path\nimport requests\n"
        )
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        by_pkg = {pkg: line for pkg, _, line in result}
        assert by_pkg["os"] == 1
        assert by_pkg["pathlib"] == 3
        assert by_pkg["requests"] == 4

    def test_file_paths(self, tmp_path):
        """Each import records the correct file path."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import os\n")
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        filepaths = {fp for _, fp, _ in result}
        assert len(filepaths) == 1
        assert filepaths.pop().endswith("lib.py")

    def test_multiple_files(self, tmp_path):
        """Imports from all Python files in the directory are collected."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "a.py").write_text("import os\n")
        (tmp_path / "b.py").write_text("import json\nimport sys\n")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "c.py").write_text("from collections import OrderedDict\n")
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"os", "json", "sys", "collections"}

    def test_empty_project(self, tmp_path):
        """A project with no Python files returns an empty set."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        assert result == set()

    def test_aliased_import(self, tmp_path):
        """import numpy as np extracts the top-level name 'numpy'."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import numpy as np\n")
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "numpy" in pkg_names

    def test_dotted_import(self, tmp_path):
        """import os.path extracts the top-level name 'os'."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import os.path\n")
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"os"}

    def test_from_dotted_import(self, tmp_path):
        """from os.path import join extracts the top-level name 'os'."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("from os.path import join\n")
        linter = PythonAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"os"}


class TestNpmImportExtraction:
    """Extract imports from JS/TS source files using tree-sitter AST."""

    def test_es_import(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "lib.js").write_text("import express from 'express';\n")
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "express" in pkg_names

    def test_require(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "lib.js").write_text("const fs = require('fs');\n")
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "fs" in pkg_names

    def test_dynamic_import(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "lib.js").write_text("const mod = import('lodash');\n")
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "lodash" in pkg_names

    def test_export_from(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "lib.js").write_text("export { handler } from 'express';\n")
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "express" in pkg_names

    def test_multiple_files(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "a.js").write_text("import express from 'express';\n")
        (tmp_path / "b.ts").write_text("import { readFile } from 'fs';\n")
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"express", "fs"}

    def test_empty_project(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        linter = NpmAstLinter()
        result = linter.scan_imports(str(tmp_path))
        assert result == set()


class TestScanImportsTopLevel:
    """Test the top-level scan_imports() function from rlsbl.lint."""

    def test_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import os\nimport json\n")
        result = scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert pkg_names == {"os", "json"}

    def test_npm_project(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        (tmp_path / "lib.js").write_text("import express from 'express';\n")
        result = scan_imports(str(tmp_path))
        pkg_names = {pkg for pkg, _, _ in result}
        assert "express" in pkg_names

    def test_no_language_markers(self, tmp_path):
        """A directory with no language markers returns empty set."""
        (tmp_path / "readme.txt").write_text("hello")
        result = scan_imports(str(tmp_path))
        assert result == set()

    def test_empty_directory(self, tmp_path):
        result = scan_imports(str(tmp_path))
        assert result == set()


class TestProtocolConformance:
    """Verify linters satisfy the ImportScanner protocol."""

    def test_python_linter_is_import_scanner(self):
        assert isinstance(PythonAstLinter(), ImportScanner)

    def test_npm_linter_is_import_scanner(self):
        assert isinstance(NpmAstLinter(), ImportScanner)
