"""Tests for rlsbl.utils.read_go_module_path -- parsing module path from go.mod files."""

import os
from unittest.mock import patch

import pytest

from rlsbl.utils import read_go_module_path


class TestReadGoModulePath:
    """Tests for read_go_module_path(project_dir)."""

    def test_valid_go_mod(self, tmp_path):
        """A standard go.mod with a module line returns the module path."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module github.com/user/repo\n\ngo 1.21\n")
        assert read_go_module_path(str(tmp_path)) == "github.com/user/repo"

    def test_missing_go_mod(self, tmp_path):
        """A directory with no go.mod returns None."""
        assert read_go_module_path(str(tmp_path)) is None

    def test_no_module_line(self, tmp_path):
        """A go.mod that exists but has no module line returns None."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("go 1.21\n\nrequire (\n)\n")
        assert read_go_module_path(str(tmp_path)) is None

    def test_whitespace_before_module_line(self, tmp_path):
        """A go.mod with blank lines and comments before the module line."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text(
            "// This is a comment\n"
            "\n"
            "   module   github.com/org/project   \n"
            "\n"
            "go 1.22\n"
        )
        assert read_go_module_path(str(tmp_path)) == "github.com/org/project"

    def test_os_error(self, tmp_path):
        """An unreadable go.mod (OSError) returns None."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module github.com/user/repo\n")
        with patch("builtins.open", side_effect=OSError("permission denied")):
            assert read_go_module_path(str(tmp_path)) is None

    def test_unicode_decode_error(self, tmp_path):
        """A go.mod with invalid encoding returns None."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_bytes(b"module \xff\xfe\n")
        assert read_go_module_path(str(tmp_path)) is None

    def test_simple_module_name(self, tmp_path):
        """A module path without slashes (single segment)."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module mymod\n")
        assert read_go_module_path(str(tmp_path)) == "mymod"

    def test_module_line_not_first(self, tmp_path):
        """The module line is not the first line in the file."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text(
            "// Copyright 2024\n"
            "// License: MIT\n"
            "\n"
            "module github.com/deep/nested/path\n"
            "\n"
            "go 1.21\n"
        )
        assert read_go_module_path(str(tmp_path)) == "github.com/deep/nested/path"

    def test_nonexistent_directory(self):
        """A completely nonexistent directory returns None."""
        assert read_go_module_path("/nonexistent/path/that/does/not/exist") is None
