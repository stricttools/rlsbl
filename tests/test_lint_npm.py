"""Tests for npm linting -- library boundary violations in JS/TS source files."""

import json

import pytest

from rlsbl.lint import lint_library
from rlsbl.lint.config import LanguageLintConfig
from rlsbl.lint.npm_ast import NpmAstLinter
from rlsbl.lint.npm_regex import NpmRegexLinter


def _make_npm_project(tmp_path, js_source=None, js_filename="lib.js",
                      package_json=None):
    """Create a minimal npm project with package.json and optional source file."""
    if package_json is None:
        package_json = {"name": "my-lib", "version": "1.0.0"}
    (tmp_path / "package.json").write_text(json.dumps(package_json))
    if js_source is not None:
        (tmp_path / js_filename).write_text(js_source)


def _default_config(**overrides):
    """Create a default npm lint config."""
    kwargs = {
        "forbidden_imports": ["express", "koa", "hono", "commander", "yargs"],
    }
    kwargs.update(overrides)
    return LanguageLintConfig(**kwargs)


@pytest.fixture(params=[NpmAstLinter, NpmRegexLinter], ids=["ast", "regex"])
def linter(request):
    return request.param()


class TestForbiddenImport:
    """Detect forbidden package imports in JS/TS files."""

    def test_es_import(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import express from 'express';\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "forbidden-import"
        assert r.severity == "error"
        assert "express" in r.message

    def test_named_import(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import { Router } from 'express';\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "express" in results[0].message

    def test_require(self, tmp_path, linter):
        _make_npm_project(tmp_path, "const app = require('express');\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "express" in results[0].message

    def test_dynamic_import(self, tmp_path, linter):
        _make_npm_project(tmp_path, "const mod = import('express');\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "express" in results[0].message

    def test_export_from(self, tmp_path, linter):
        _make_npm_project(tmp_path, "export { handler } from 'express';\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "express" in results[0].message

    def test_allowed_import(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import path from 'path';\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_typescript_file(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import { Command } from 'commander';\n", "cli.ts")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "commander" in results[0].message

    def test_tsx_file(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import express from 'express';\n", "app.tsx")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "express" in results[0].message

    def test_mjs_file(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import koa from 'koa';\n", "lib.mjs")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "koa" in results[0].message

    def test_cjs_file(self, tmp_path, linter):
        _make_npm_project(tmp_path, "const koa = require('koa');\n", "lib.cjs")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "koa" in results[0].message

    def test_integration_via_lint_library(self, tmp_path):
        """lint_library() detects npm projects via package.json."""
        _make_npm_project(tmp_path, "import express from 'express';\n")
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1
        assert "express" in forbidden[0].message


class TestStdoutDetection:
    """Detect console.log/warn/error/info calls."""

    def test_console_log(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.log('hello');\n")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert r.severity == "error"
        assert "console.log()" in r.message

    def test_console_warn(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.warn('warning');\n")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "console.warn()" in results[0].message

    def test_console_error(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.error('err');\n")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "console.error()" in results[0].message

    def test_console_info(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.info('info');\n")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "console.info()" in results[0].message

    def test_stdout_disabled(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.log('hello');\n")
        config = _default_config(forbidden_imports=[], stdout_enabled=False)
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_stdout_ignore_console(self, tmp_path, linter):
        _make_npm_project(tmp_path, "console.log('hello');\n")
        config = _default_config(forbidden_imports=[], stdout_ignore=["console"])
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestEntryPoint:
    """Detect CLI entry points in package.json bin field."""

    def test_bin_string(self, tmp_path, linter):
        _make_npm_project(tmp_path, package_json={
            "name": "my-cli",
            "version": "1.0.0",
            "bin": "./cli.js",
        })
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "entry-point"
        assert r.severity == "error"
        assert "my-cli" in r.message

    def test_bin_dict(self, tmp_path, linter):
        _make_npm_project(tmp_path, package_json={
            "name": "my-lib",
            "version": "1.0.0",
            "bin": {"mycli": "./cli.js"},
        })
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "mycli" in results[0].message

    def test_no_bin(self, tmp_path, linter):
        _make_npm_project(tmp_path)
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_entry_point_disabled(self, tmp_path, linter):
        _make_npm_project(tmp_path, package_json={
            "name": "my-cli",
            "version": "1.0.0",
            "bin": "./cli.js",
        })
        config = _default_config(forbidden_imports=[], entry_point_enabled=False)
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_entry_point_ignore(self, tmp_path, linter):
        _make_npm_project(tmp_path, package_json={
            "name": "my-lib",
            "version": "1.0.0",
            "bin": {"mycli": "./cli.js"},
        })
        config = _default_config(forbidden_imports=[], entry_point_ignore=["mycli"])
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestCleanProject:
    """An npm project with no violations returns an empty list."""

    def test_clean(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import path from 'path';\n\nexport function upper(s) {\n  return s.toUpperCase();\n}\n")
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestConfigSuppression:
    """Config-based suppression of rules."""

    def test_custom_forbidden_imports(self, tmp_path, linter):
        """Only the specified imports are forbidden."""
        _make_npm_project(tmp_path, "import express from 'express';\n")
        config = _default_config(forbidden_imports=["commander"])
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_empty_forbidden_imports(self, tmp_path, linter):
        _make_npm_project(tmp_path, "import express from 'express';\n")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert results == []
