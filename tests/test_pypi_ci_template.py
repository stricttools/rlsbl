"""Tests for the pypi CI template: locked sync, pytest step, no UV_NO_SOURCES.

The scaffolded CI uses ``uv sync --locked`` (registry-pure lockfiles are
guaranteed by the cross-repo-path-sources ban, so the lockfile always
resolves in CI) and runs ``uv run pytest`` after the import smoke test when
the project declares pytest (detected by the same probe the release test
runner uses). The UV_NO_SOURCES escape hatch is gone: committed path
sources are banned outright instead of being masked in CI.
"""

import os

from conftest import make_ctx
from rlsbl.commands.init_cmd import process_template
from rlsbl.commands.monorepo import parse_ci_workflow
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


PYPROJECT_WITH_PYTEST = """\
[project]
name = "mylib"
version = "0.1.0"

[dependency-groups]
dev = ["pytest>=8.0"]
"""

PYPROJECT_WITHOUT_PYTEST = """\
[project]
name = "mylib"
version = "0.1.0"
"""


class TestTemplateVarsPytestProbe:
    """template_vars sets hasPytest from the pytest-location probe."""

    def test_pytest_in_dependency_groups(self, tmp_path):
        _write_pyproject(tmp_path, PYPROJECT_WITH_PYTEST)
        vars = PypiTarget().template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["hasPytest"] == "true"

    def test_pytest_in_optional_dependencies(self, tmp_path):
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[project.optional-dependencies]
test = ["pytest"]
""")
        vars = PypiTarget().template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["hasPytest"] == "true"

    def test_no_pytest_declared(self, tmp_path):
        _write_pyproject(tmp_path, PYPROJECT_WITHOUT_PYTEST)
        vars = PypiTarget().template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "hasPytest" not in vars

    def test_uv_no_sources_var_gone(self, tmp_path):
        """The uvNoSources template var is never produced anymore."""
        _write_pyproject(tmp_path, """\
[project]
name = "mylib"
version = "0.1.0"

[tool.uv.sources]
strictcli = { path = "../strictcli", editable = true }
""")
        vars = PypiTarget().template_vars(str(tmp_path), make_ctx(tmp_path))
        assert "uvNoSources" not in vars


class TestCiTemplateRendering:
    """Rendered CI content for probe-positive and probe-negative projects."""

    def test_probe_positive_has_locked_and_pytest(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "pypi.hasPytest": "true",
            "importName": "mylib",
        })
        assert "uv sync --locked" in content
        assert "- run: uv run pytest" in content
        # pytest runs after the import smoke test
        assert content.index("uv run python -c") < content.index("uv run pytest")

    def test_probe_negative_has_locked_no_pytest(self):
        template = _read_ci_template()
        content, _ = process_template(template, {
            "importName": "mylib",
        })
        assert "uv sync --locked" in content
        assert "uv run pytest" not in content

    def test_no_uv_no_sources_anywhere(self):
        """Neither the template nor any rendering emits UV_NO_SOURCES."""
        template = _read_ci_template()
        assert "UV_NO_SOURCES" not in template
        assert "uvNoSources" not in template
        for vars in ({"importName": "mylib"},
                     {"pypi.hasPytest": "true", "importName": "mylib"}):
            content, _ = process_template(template, dict(vars))
            assert "UV_NO_SOURCES" not in content
            assert "env:" not in content

    def test_bare_uv_sync_gone(self):
        """The unlocked ``uv sync`` invocation no longer exists."""
        template = _read_ci_template()
        for line in template.splitlines():
            if "uv sync" in line:
                assert "--locked" in line


class TestCiTemplateYamlStructure:
    """Rendered CI outputs must parse as YAML (ruamel round-trip, matching
    test_ci_concurrency conventions); pytest step presence is verified on
    the parsed job structure, not raw strings."""

    def _render(self, vars):
        content, _ = process_template(_read_ci_template(), vars)
        return content

    def _run_steps(self, content):
        doc = parse_ci_workflow(content)
        assert doc is not None, "rendered CI is not a valid workflow document"
        steps = doc["jobs"]["test"]["steps"]
        return [step["run"] for step in steps if "run" in step]

    def test_probe_positive_parses_with_pytest_step(self):
        content = self._render({"pypi.hasPytest": "true", "importName": "mylib"})
        runs = self._run_steps(content)
        # The pytest invocation pins --rootdir . so monorepo members never let
        # rootdir escape to the workspace root.
        assert "uv run pytest --rootdir ." in runs
        # pytest step comes after the import smoke test in the parsed steps
        smoke_idx = next(
            i for i, r in enumerate(runs) if r.startswith("uv run python -c")
        )
        assert runs.index("uv run pytest --rootdir .") > smoke_idx

    def test_probe_negative_parses_without_pytest_step(self):
        content = self._render({"importName": "mylib"})
        runs = self._run_steps(content)
        assert not any("pytest" in r for r in runs)
        # the rest of the job survives rendering intact
        assert "uv sync --locked" in runs
