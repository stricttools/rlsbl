"""Tests for lint config parser selection and multi-language support."""

import json

import pytest

from rlsbl.errors import ConfigError
from rlsbl.lint import lint_library
from rlsbl.lint.config import load_language_config, load_parser_setting


class TestLoadParserSetting:
    """load_parser_setting: missing file defaults; invalid present values hard-error."""

    def test_missing_file_defaults_to_ast(self, tmp_path):
        assert load_parser_setting(str(tmp_path)) == "ast"

    def test_valid_regex(self, tmp_path):
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text('parser = "regex"\n')
        assert load_parser_setting(str(tmp_path)) == "regex"

    def test_invalid_parser_value_raises(self, tmp_path):
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text('parser = "bogus"\n')
        with pytest.raises(ConfigError) as exc_info:
            load_parser_setting(str(tmp_path))
        msg = str(exc_info.value)
        assert "parser" in msg
        assert "bogus" in msg

    def test_malformed_toml_raises(self, tmp_path):
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text("this is = = not valid toml\n")
        with pytest.raises(ConfigError):
            load_parser_setting(str(tmp_path))


class TestLoadLanguageConfig:
    """load_language_config: missing file defaults; malformed TOML hard-errors."""

    def test_missing_file_defaults(self, tmp_path):
        cfg = load_language_config(str(tmp_path), "python")
        assert "argparse" in cfg.forbidden_imports

    def test_malformed_toml_raises(self, tmp_path):
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text("this is = = not valid toml\n")
        with pytest.raises(ConfigError):
            load_language_config(str(tmp_path), "python")


class TestParserSelection:
    """Verify parser = 'regex' selects the regex backend."""

    def test_regex_parser_detects_in_comments(self, tmp_path):
        """Regex backend matches patterns even in comments (AST would not)."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text('parser = "regex"\n')
        # A line starting with 'import argparse' inside a docstring comment
        # is detected by regex but not by AST
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\n')
        (tmp_path / "lib.py").write_text(
            '"""\nimport argparse\n"""\n'
        )
        results = lint_library(str(tmp_path))
        # Regex backend sees the line-level pattern
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1

    def test_ast_parser_ignores_in_comments(self, tmp_path):
        """AST backend does NOT match patterns inside string literals."""
        rlsbl_dir = tmp_path / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "lint.toml").write_text('parser = "ast"\n')
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\n')
        (tmp_path / "lib.py").write_text(
            '"""\nimport argparse\n"""\n'
        )
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 0


class TestMultiLanguageProject:
    """Projects with both pyproject.toml and package.json run both linters."""

    def test_python_and_npm_violations(self, tmp_path):
        """Both Python and npm violations are reported in combined results."""
        # Python project marker + violation
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "example"\n'
        )
        (tmp_path / "lib.py").write_text("import argparse\n")

        # npm project marker + violation
        pkg = {"name": "example", "version": "1.0.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        (tmp_path / "index.js").write_text(
            "const express = require('express');\n"
        )

        results = lint_library(str(tmp_path))

        # Should have violations from both languages
        python_results = [
            r for r in results
            if r.file.endswith(".py") or r.file.endswith("pyproject.toml")
        ]
        npm_results = [
            r for r in results
            if r.file.endswith(".js") or r.file.endswith("package.json")
        ]
        assert len(python_results) >= 1, f"Expected Python violations, got: {results}"
        assert len(npm_results) >= 1, f"Expected npm violations, got: {results}"

    def test_both_clean(self, tmp_path):
        """Clean multi-language project returns empty results."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "example"\n'
        )
        (tmp_path / "lib.py").write_text("import os\n")

        pkg = {"name": "example", "version": "1.0.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        (tmp_path / "index.js").write_text("const x = 1;\n")

        results = lint_library(str(tmp_path))
        assert results == []
