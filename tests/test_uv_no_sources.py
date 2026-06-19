"""Tests for UV_NO_SOURCES scaffold-time source analysis.

Verifies that PypiTarget.template_vars() sets uvNoSources when pyproject.toml
has path-based uv sources, and that the CI template conditionally renders the
UV_NO_SOURCES env block based on this variable.
"""

import os

from conftest import make_ctx
from rlsbl.commands.init_cmd import process_template
from rlsbl.targets.pypi import PypiTarget


def _write_pyproject(tmp_path, content):
    (tmp_path / "pyproject.toml").write_text(content)
    # template_vars needs a package directory for import name detection
    pkg_dir = tmp_path / "mylib"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text('__version__ = "0.1.0"\n')


def _read_ci_template():
    target = PypiTarget()
    tpl_path = os.path.join(target.template_dir(), "ci.yml.tpl")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Phase 3.1: template_vars source analysis
# ---------------------------------------------------------------------------


class TestTemplateVarsPathSources:
    """template_vars returns uvNoSources='true' when path sources exist."""

    def test_path_source_sets_uv_no_sources(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv.sources]
strictcli = { path = "../strictcli", editable = true }
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["uvNoSources"] == "true"

    def test_multiple_sources_with_one_path(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv.sources]
requests = { workspace = true }
strictcli = { path = "../strictcli", editable = true }
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["uvNoSources"] == "true"


class TestTemplateVarsWorkspaceSources:
    """template_vars does NOT return uvNoSources for workspace-only sources."""

    def test_workspace_sources_no_uv_no_sources(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv.sources]
strictcli = { workspace = true }
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "uvNoSources" not in vars

    def test_multiple_workspace_sources(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv.sources]
strictcli = { workspace = true }
tomlkit = { workspace = true }
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "uvNoSources" not in vars


class TestTemplateVarsNoSources:
    """template_vars does NOT return uvNoSources when no sources section."""

    def test_no_sources_section(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "uvNoSources" not in vars

    def test_empty_uv_section(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv]
""")
        target = PypiTarget()
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "uvNoSources" not in vars


# ---------------------------------------------------------------------------
# Phase 3.2: CI template conditional rendering
# ---------------------------------------------------------------------------


class TestCiTemplateWithPathSources:
    """CI template renders UV_NO_SOURCES env block when uvNoSources is set."""

    def test_renders_uv_no_sources_env(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "uvNoSources": "true",
            "importName": "mylib",
        })
        assert 'UV_NO_SOURCES: "1"' in content
        assert "env:" in content

    def test_env_block_before_jobs(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "uvNoSources": "true",
            "importName": "mylib",
        })
        env_pos = content.index("env:")
        jobs_pos = content.index("jobs:")
        assert env_pos < jobs_pos


class TestCiTemplateWithoutPathSources:
    """CI template omits UV_NO_SOURCES env block when uvNoSources is absent."""

    def test_no_uv_no_sources_env(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "importName": "mylib",
        })
        assert "UV_NO_SOURCES" not in content

    def test_no_env_block(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "importName": "mylib",
        })
        assert "env:" not in content
