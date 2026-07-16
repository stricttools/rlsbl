"""Tests for the pytest rootdir pin and the member-pytest-config check.

Monorepo members that ship tests but declare no own
``[tool.pytest.ini_options]`` while the workspace root carries a
``conftest.py`` are hit by pytest's rootdir discovery escaping to the
workspace root: the member silently loads the root conftest and its config.
The CI templates pin ``--rootdir .`` and the ``member-pytest-config`` check
blocks the hazard at the workspace level.
"""

import os

import pytest

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)


class TestPytestRootdirPin:
    """CI templates pin pytest's rootdir so members never escape to the root."""

    def test_pypi_ci_template_pins_rootdir(self):
        tpl = os.path.join(TEMPLATES_ROOT, "pypi", "ci.yml.tpl")
        with open(tpl, encoding="utf-8") as f:
            content = f.read()
        assert "uv run pytest --rootdir ." in content
        # The bare invocation must be gone (would let rootdir escape).
        assert "uv run pytest{{/if}}" not in content

    def test_inlined_router_preserves_rootdir_pin(self):
        """A member CI whose pytest step is pinned keeps the pin after inlining."""
        from rlsbl.commands.monorepo import _generate_router, parse_ci_workflow

        ci = (
            "name: CI\n"
            "on:\n  push:\n    branches: [main]\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: uv run pytest --rootdir .\n"
        )
        doc = parse_ci_workflow(ci)
        projects = [{
            "name": "mypkg",
            "path": "mypkg",
            "_ci_docs": [("mypkg-ci", doc)],
            "_ci_files": ["mypkg-ci.yml"],
        }]
        content = _generate_router(projects)
        assert "uv run pytest --rootdir ." in content


def _ctx(root, projects):
    return WorkspaceCheckContext(
        project_root=root,
        workspace_root=root,
        config={},
        projects=projects,
        graph=None,
    )


def _member(root, name, *, tests=True, pytest_config=False, python=True):
    """Create a member package under *root*."""
    pkg = os.path.join(str(root), name)
    os.makedirs(pkg, exist_ok=True)
    if python:
        content = f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
        if pytest_config:
            content += '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
        with open(os.path.join(pkg, "pyproject.toml"), "w") as f:
            f.write(content)
    if tests:
        os.makedirs(os.path.join(pkg, "tests"), exist_ok=True)
        with open(os.path.join(pkg, "tests", "test_x.py"), "w") as f:
            f.write("def test_x():\n    assert True\n")
    return {"path": name, "name": name}


class TestMemberPytestConfigCheck:
    """The member-pytest-config check detects rootdir-escape hazards."""

    def test_skips_without_root_conftest(self, mock_git_repo):
        """No root conftest.py -> no hazard -> skip."""
        proj = _member(mock_git_repo, "mylib", tests=True, pytest_config=False)
        result = app._check_defs["member-pytest-config"].impl(
            _ctx(mock_git_repo, [proj])
        )
        assert result.status == "skip"

    def test_fails_member_without_pytest_config(self, mock_git_repo):
        """Root conftest + member with tests but no own pytest config -> fail."""
        (mock_git_repo / "conftest.py").write_text("")
        proj = _member(mock_git_repo, "mylib", tests=True, pytest_config=False)
        result = app._check_defs["member-pytest-config"].impl(
            _ctx(mock_git_repo, [proj])
        )
        assert result.status == "fail"
        assert "mylib" in " ".join(p.text for p in result.problems) + result.message

    def test_passes_member_with_pytest_config(self, mock_git_repo):
        """Member declaring its own [tool.pytest.ini_options] -> pass."""
        (mock_git_repo / "conftest.py").write_text("")
        proj = _member(mock_git_repo, "mylib", tests=True, pytest_config=True)
        result = app._check_defs["member-pytest-config"].impl(
            _ctx(mock_git_repo, [proj])
        )
        assert result.status == "pass"

    def test_passes_member_without_tests(self, mock_git_repo):
        """Member with no tests -> no hazard even without pytest config."""
        (mock_git_repo / "conftest.py").write_text("")
        proj = _member(mock_git_repo, "mylib", tests=False, pytest_config=False)
        result = app._check_defs["member-pytest-config"].impl(
            _ctx(mock_git_repo, [proj])
        )
        assert result.status == "pass"

    def test_root_path_member_exempt(self, mock_git_repo):
        """A path='.' member shares the root conftest and is exempt."""
        (mock_git_repo / "conftest.py").write_text("")
        (mock_git_repo / "pyproject.toml").write_text(
            '[project]\nname = "root"\nversion = "0.1.0"\n'
        )
        os.makedirs(os.path.join(str(mock_git_repo), "tests"), exist_ok=True)
        (mock_git_repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        result = app._check_defs["member-pytest-config"].impl(
            _ctx(mock_git_repo, [{"path": ".", "name": "root"}])
        )
        assert result.status == "pass"

    def test_severity_is_error(self):
        """The check is a hard error (no bypass)."""
        assert app._check_defs["member-pytest-config"].severity == "error"
