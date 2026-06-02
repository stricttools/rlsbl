"""Tests for rlsbl.lint -- library boundary linting."""

from rlsbl.lint import LintResult, lint_library

# Helper to create a minimal Python project marker
_PYPROJECT = '[project]\nname = "example"\n'


class TestForbiddenImport:
    """Detect forbidden module imports."""

    def test_import_argparse(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        r = results[0]
        assert r.rule == "forbidden-import"
        assert r.severity == "error"
        assert "argparse" in r.message

    def test_from_flask_import(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "app.py"
        src.write_text("from flask import Flask\n")
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        r = results[0]
        assert r.rule == "forbidden-import"
        assert r.severity == "error"
        assert "flask" in r.message

    def test_import_os_allowed(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text("import os\n")
        results = lint_library(str(tmp_path))
        assert results == []

    def test_import_json_allowed(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text("import json\n")
        results = lint_library(str(tmp_path))
        assert results == []

    def test_no_language_markers_returns_empty(self, tmp_path):
        """When no language markers exist, lint returns empty results."""
        src = tmp_path / "lib.py"
        src.write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert results == []


class TestStdoutDetection:
    """Detect print(), sys.stdout/stderr.write(), and logging calls."""

    def test_print_call(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text('print("hello")\n')
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert r.severity == "error"
        assert "print()" in r.message

    def test_sys_stdout_write(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text('import sys\nsys.stdout.write("x")\n')
        results = lint_library(str(tmp_path))
        # One stdout violation (sys is not a forbidden import).
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert r.severity == "error"
        assert "sys.stdout" in r.message

    def test_logging_info(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src = tmp_path / "lib.py"
        src.write_text('import logging\nlogging.info("x")\n')
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert r.severity == "warning"
        assert "logging" in r.message


class TestEntryPoint:
    """Detect CLI entry point declarations in pyproject.toml."""

    def test_scripts_section(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\n\n'
            '[project.scripts]\nmycli = "example:main"\n'
        )
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        r = results[0]
        assert r.rule == "entry-point"
        assert r.severity == "error"
        assert "mycli" in r.message
        assert r.line == 0

    def test_no_scripts(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "example"\n')
        results = lint_library(str(tmp_path))
        assert results == []


class TestTestFileInclusion:
    """Test and example files are excluded from lint by default."""

    def test_tests_dir_excluded_by_default(self, tmp_path):
        """Without exclude config, test files are NOT linted (default exclusion)."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text('print("in test")\n')
        results = lint_library(str(tmp_path))
        assert results == []

    def test_tests_dir_excluded_by_config(self, tmp_path):
        """With exclude config, test files are skipped (user overrides merge with defaults)."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text('print("in test")\n')
        # Configure exclusion -- user adds extra pattern, defaults still apply
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text(
            '[files]\nexclude = ["custom_dir/"]\n'
        )
        results = lint_library(str(tmp_path))
        assert results == []

    def test_src_file_not_excluded(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text('print("in src")\n')
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        assert results[0].rule == "stdout"

    def test_go_test_file_excluded_by_default(self, tmp_path):
        """Go test files (*_test.go) are excluded from lint by default."""
        (tmp_path / "go.mod").write_text("module example\n\ngo 1.21\n")
        (tmp_path / "lib.go").write_text(
            'package example\n\nimport "net/http"\n'
        )
        (tmp_path / "lib_test.go").write_text(
            'package example\n\nimport "net/http"\n'
        )
        results = lint_library(str(tmp_path))
        # Only the production file should produce a violation
        assert len(results) == 1
        assert "lib.go" in results[0].file

    def test_npm_test_file_excluded_by_default(self, tmp_path):
        """npm test files (*.test.ts) are excluded from lint by default."""
        (tmp_path / "package.json").write_text('{"name": "example"}\n')
        (tmp_path / "lib.ts").write_text('import express from "express";\n')
        (tmp_path / "lib.test.ts").write_text(
            'import express from "express";\n'
        )
        results = lint_library(str(tmp_path))
        # Only the production file should produce a violation
        assert len(results) == 1
        assert "lib.ts" in results[0].file
        assert "lib.test.ts" not in results[0].file


class TestLanguageConfig:
    """Per-language config in .rlsbl/lint/<language>.toml controls linting."""

    def test_custom_forbidden_imports(self, tmp_path):
        """Config can narrow the forbidden list to only specified modules."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text(
            '[forbidden-imports]\nmodules = ["flask"]\n'
        )
        # argparse is no longer forbidden with custom config
        (tmp_path / "lib.py").write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert results == []

    def test_stdout_disabled(self, tmp_path):
        """stdout checking can be disabled via config."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text("[stdout]\nenabled = false\n")
        (tmp_path / "lib.py").write_text('print("hello")\n')
        results = lint_library(str(tmp_path))
        assert results == []

    def test_stdout_ignore_print(self, tmp_path):
        """stdout ignore list can suppress print detection."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text(
            '[stdout]\nenabled = true\nignore = ["print"]\n'
        )
        (tmp_path / "lib.py").write_text('print("hello")\n')
        results = lint_library(str(tmp_path))
        assert results == []

    def test_entry_point_disabled(self, tmp_path):
        """Entry point checking can be disabled via config."""
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text("[entry-point]\nenabled = false\n")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\n\n'
            '[project.scripts]\nmycli = "example:main"\n'
        )
        results = lint_library(str(tmp_path))
        assert results == []

    def test_entry_point_ignore(self, tmp_path):
        """Entry point ignore list can suppress specific entry points."""
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text(
            '[entry-point]\nenabled = true\nignore = ["mycli"]\n'
        )
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "example"\n\n'
            '[project.scripts]\nmycli = "example:main"\n'
        )
        results = lint_library(str(tmp_path))
        assert results == []


class TestParserSetting:
    """The .rlsbl/lint.toml parser setting selects AST or regex linter."""

    def test_default_is_ast(self, tmp_path):
        """Without lint.toml, the AST linter is used (default)."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        assert results[0].rule == "forbidden-import"

    def test_regex_parser(self, tmp_path):
        """parser = 'regex' in lint.toml uses the regex linter."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text('parser = "regex"\n')
        (tmp_path / "lib.py").write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        assert results[0].rule == "forbidden-import"


class TestDirectoryExclusion:
    """Files in excluded directories (e.g., .venv) should not be linted."""

    def test_venv_excluded(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        venv_dir = tmp_path / ".venv" / "somepkg"
        venv_dir.mkdir(parents=True)
        (venv_dir / "bad.py").write_text("import flask\n")
        results = lint_library(str(tmp_path))
        assert results == [], f"Expected no findings from .venv, got: {results}"

    def test_node_modules_excluded(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        nm_dir = tmp_path / "node_modules" / "somepkg"
        nm_dir.mkdir(parents=True)
        (nm_dir / "bad.py").write_text("import flask\n")
        results = lint_library(str(tmp_path))
        assert results == []

    def test_egg_info_excluded(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        egg_dir = tmp_path / "mypkg.egg-info"
        egg_dir.mkdir()
        (egg_dir / "bad.py").write_text("import flask\n")
        results = lint_library(str(tmp_path))
        assert results == []

    def test_source_still_linted(self, tmp_path):
        """Non-excluded directories are still linted normally."""
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "lib.py").write_text("import flask\n")
        results = lint_library(str(tmp_path))
        assert len(results) == 1
        assert results[0].rule == "forbidden-import"


class TestCleanLibrary:
    """A project with no violations returns an empty list."""

    def test_clean(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "lib.py").write_text("import os\nimport json\n\ndef add(a, b):\n    return a + b\n")
        results = lint_library(str(tmp_path))
        assert results == []
