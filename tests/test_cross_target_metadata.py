"""Tests for cross-target metadata consistency (Phases 1-4).

Covers: process_template dotted vars, normalize functions, min-version extraction,
read_name/read_metadata, _merge_template_vars,
and scaffold preservation of version-reference comments.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from conftest import make_ctx
from rlsbl.commands.init_cmd import (
    process_template,
    process_mappings,
    _merge_template_vars,
    _load_base,
)
from rlsbl.targets import TARGETS
from rlsbl.targets.utils import normalize_npm, normalize_pypi, normalize_go


# ---------------------------------------------------------------------------
# Test class 1: process_template with dotted variable support
# ---------------------------------------------------------------------------


class TestProcessTemplateDotted:
    """Unit tests for the updated process_template regex supporting dotted names."""

    def test_simple_var(self):
        content, unreplaced = process_template("Hello {{name}}", {"name": "foo"})
        assert content == "Hello foo"
        assert unreplaced == []

    def test_dotted_var(self):
        content, unreplaced = process_template(
            "v={{pypi.minRequiredPython}}", {"pypi.minRequiredPython": "3.11"}
        )
        assert content == "v=3.11"
        assert unreplaced == []

    def test_dotted_var_unreplaced(self):
        content, unreplaced = process_template("v={{pypi.minRequiredPython}}", {})
        assert content == "v={{pypi.minRequiredPython}}"
        assert "pypi.minRequiredPython" in unreplaced

    def test_goreleaser_not_matched(self):
        """GoReleaser's {{.Version}} must not be matched."""
        content, unreplaced = process_template("{{.Version}}", {"Version": "1.0"})
        assert content == "{{.Version}}"  # unchanged

    def test_goreleaser_spaced_not_matched(self):
        content, unreplaced = process_template("{{ .ProjectName }}", {})
        assert content == "{{ .ProjectName }}"

    def test_multi_level_dotted(self):
        content, unreplaced = process_template("{{a.b.c}}", {"a.b.c": "val"})
        assert content == "val"

    def test_mixed_simple_and_dotted(self):
        content, unreplaced = process_template(
            "{{name}} uses {{pypi.minRequiredPython}}",
            {"name": "foo", "pypi.minRequiredPython": "3.11"},
        )
        assert content == "foo uses 3.11"


# ---------------------------------------------------------------------------
# Test class 2: normalize functions
# ---------------------------------------------------------------------------


class TestNormalizeFunctions:
    """Unit tests for name-normalization utilities in targets/utils.py."""

    def test_normalize_npm_strips_special(self):
        assert normalize_npm("my-pkg") == normalize_npm("my_pkg") == normalize_npm("my.pkg")

    def test_normalize_npm_lowercases(self):
        assert normalize_npm("MyPkg") == "mypkg"

    def test_normalize_pypi_pep503(self):
        assert normalize_pypi("My_Pkg") == "my-pkg"
        assert normalize_pypi("my--pkg") == "my-pkg"

    def test_normalize_go_last_segment(self):
        assert normalize_go("github.com/user/my-repo") == "my-repo"
        assert normalize_go("my-repo") == "my-repo"

    def test_normalize_go_lowercases(self):
        assert normalize_go("github.com/User/MyRepo") == "myrepo"


# ---------------------------------------------------------------------------
# Test class 3: min-version extraction from template_vars
# ---------------------------------------------------------------------------


class TestMinVersionExtraction:
    """Test the min-version regex extraction from target template_vars."""

    def test_pypi_min_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'
        )
        vars_ = TARGETS["pypi"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredPython"] == "3.11"

    def test_pypi_min_python_with_upper_bound(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\nrequires-python = ">=3.11,<4"\n'
        )
        vars_ = TARGETS["pypi"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredPython"] == "3.11"

    def test_pypi_no_requires_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )
        vars_ = TARGETS["pypi"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "minRequiredPython" not in vars_

    def test_npm_min_node(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0", "engines": {"node": ">=18"}}'
        )
        vars_ = TARGETS["npm"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredNode"] == "18"

    def test_npm_min_node_with_minor(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0", "engines": {"node": ">=18.0.0"}}'
        )
        vars_ = TARGETS["npm"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredNode"] == "18.0.0"

    def test_npm_no_engines(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0"}'
        )
        vars_ = TARGETS["npm"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "minRequiredNode" not in vars_

    def test_go_min_version(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/test\n\ngo 1.21\n")
        (tmp_path / "VERSION").write_text("1.0.0")
        vars_ = TARGETS["go"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredGo"] == "1.21"

    def test_cargo_min_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "test"\nversion = "1.0.0"\n'
            'rust-version = "1.70"\nedition = "2021"\n'
        )
        vars_ = TARGETS["cargo"].template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["minRequiredRust"] == "1.70"
        assert vars_["edition"] == "2021"


# ---------------------------------------------------------------------------
# Test class 4: read_name and read_metadata
# ---------------------------------------------------------------------------


class TestReadNameAndMetadata:
    """Test read_name() and read_metadata() on key targets."""

    def test_pypi_read_name(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-package"\nversion = "1.0.0"\n'
        )
        assert TARGETS["pypi"].read_name(str(tmp_path)) == "my-package"

    def test_pypi_read_metadata_string_license(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
            'license = "MIT"\ndescription = "A test"\n'
        )
        meta = TARGETS["pypi"].read_metadata(str(tmp_path))
        assert meta["license"] == "MIT"
        assert meta["description"] == "A test"

    def test_pypi_read_metadata_table_license(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n\n'
            "[project.license]\n"
            'text = "MIT"\n'
        )
        meta = TARGETS["pypi"].read_metadata(str(tmp_path))
        assert meta["license"] == "MIT"

    def test_npm_read_name(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "@scope/pkg", "version": "1.0.0"}'
        )
        assert TARGETS["npm"].read_name(str(tmp_path)) == "@scope/pkg"

    def test_npm_read_metadata(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0", "license": "ISC", "description": "Hello"}'
        )
        meta = TARGETS["npm"].read_metadata(str(tmp_path))
        assert meta["license"] == "ISC"
        assert meta["description"] == "Hello"

    def test_go_read_name(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/myrepo\n\ngo 1.21\n")
        assert TARGETS["go"].read_name(str(tmp_path)) == "myrepo"

    def test_read_name_missing_file(self, tmp_path):
        assert TARGETS["pypi"].read_name(str(tmp_path)) is None
        assert TARGETS["npm"].read_name(str(tmp_path)) is None

    def test_read_metadata_missing_file(self, tmp_path):
        assert TARGETS["pypi"].read_metadata(str(tmp_path)) == {}
        assert TARGETS["npm"].read_metadata(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# Test class 5: _merge_template_vars
# ---------------------------------------------------------------------------


class TestMergeTemplateVars:
    """Test the _merge_template_vars function from init_cmd.py."""

    def test_merge_primary_unnamespaced(self, tmp_path):
        # Create both package.json and pyproject.toml
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0"}'
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
            'requires-python = ">=3.11"\n'
        )
        target_paths = {"npm": str(tmp_path), "pypi": str(tmp_path)}
        merged = _merge_template_vars(["npm", "pypi"], "npm", target_paths, make_ctx(tmp_path))
        # Primary (npm) vars are un-namespaced
        assert "name" in merged
        # All vars are namespaced
        assert "npm.name" in merged
        assert "pypi.name" in merged
        assert "pypi.minRequiredPython" in merged

    def test_merge_year_not_included(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "version": "1.0.0"}'
        )
        target_paths = {"npm": str(tmp_path)}
        merged = _merge_template_vars(["npm"], "npm", target_paths, make_ctx(tmp_path))
        # year is added by the caller, not _merge_template_vars
        assert "year" not in merged



# ---------------------------------------------------------------------------
# Test class 7: version-reference comments in scaffold templates
# ---------------------------------------------------------------------------


class TestScaffoldUpdateVersionComments:
    """Tests that version-reference comments (e.g. '# requires-python: >= 3.11')
    render correctly and survive scaffold three-way merges."""

    def test_process_template_renders_version_comment(self):
        """process_template should render dotted vars in YAML comments."""
        template = (
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        # requires-python: >= {{pypi.minRequiredPython}}\n"
            '        python-version: ["3.12", "3.13"]\n'
        )
        vars_dict = {"pypi.minRequiredPython": "3.11"}
        content, unreplaced = process_template(template, vars_dict)
        assert "# requires-python: >= 3.11" in content
        assert unreplaced == []

    def test_process_template_version_comment_unreplaced_when_missing(self):
        """When the var is not provided, the placeholder stays and is reported."""
        template = "# requires-python: >= {{pypi.minRequiredPython}}\n"
        content, unreplaced = process_template(template, {})
        assert "{{pypi.minRequiredPython}}" in content
        assert "pypi.minRequiredPython" in unreplaced

    def test_scaffold_update_preserves_version_comment(self, mock_git_repo):
        """After scaffold + user edit + scaffold, version-reference
        comments survive the three-way merge."""
        tpl_dir = mock_git_repo / "_tpls"
        tpl_dir.mkdir()

        # Template v1 with a version-reference comment (7 lines for merge spacing)
        tpl_v1 = (
            "name: CI\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  test:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        # requires-python: >= {{pypi.minRequiredPython}}\n"
            '        python-version: ["3.12", "3.13"]\n'
            "    steps:\n"
            "      - uses: actions/checkout@v6\n"
        )
        (tpl_dir / "ci.yml.tpl").write_text(tpl_v1)

        mappings = [{"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"}]
        vars_dict = {"pypi.minRequiredPython": "3.11"}

        # Initial scaffold
        created, skipped, warnings, hashes = process_mappings(
            str(tpl_dir), mappings, vars_dict, force=False,
        )
        ci_path = mock_git_repo / ".github" / "workflows" / "ci.yml"
        assert ci_path.exists()
        initial_content = ci_path.read_text()
        assert "# requires-python: >= 3.11" in initial_content

        # Simulate user customization: add a comment at the end (non-adjacent)
        user_content = initial_content.rstrip("\n") + "\n      # user customization\n"
        ci_path.write_text(user_content)

        # Template v2: same version comment, but a different line changed elsewhere
        tpl_v2 = tpl_v1.replace("actions/checkout@v6", "actions/checkout@v7")
        (tpl_dir / "ci.yml.tpl").write_text(tpl_v2)

        # Scaffold (three-way merge)
        created2, skipped2, warnings2, hashes2 = process_mappings(
            str(tpl_dir), mappings, vars_dict, force=False,
        )

        merged_content = ci_path.read_text()
        # Version-reference comment must survive
        assert "# requires-python: >= 3.11" in merged_content
        # User customization must survive
        assert "# user customization" in merged_content
        # Template update must be applied
        assert "actions/checkout@v7" in merged_content
