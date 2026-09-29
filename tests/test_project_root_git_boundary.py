"""Project and workspace discovery stop at the enclosing git repository's root.

A repository nested inside another (a scratch repository under a tool's
experiments/ directory, a vendored checkout) is its own world: the outer
repository's .rlsbl/ or .rlsbl-monorepo/ is never its project, and walking
past its root made `rlsbl monorepo init` there refuse as "inside existing
project" of the outer one.
"""

import pytest

from conftest import run_git
from rlsbl.utils import find_project_root
from rlsbl.workspace import find_workspace_root


@pytest.fixture
def outer_and_inner(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "experiments" / "scratch"
    inner.mkdir(parents=True)
    run_git(outer, "init", "-q")
    run_git(inner, "init", "-q")
    return outer, inner


def test_a_project_marker_above_the_git_root_is_not_found(outer_and_inner):
    outer, inner = outer_and_inner
    (outer / ".rlsbl").mkdir()
    assert find_project_root(str(inner)) is None
    (inner / "sub").mkdir()
    assert find_project_root(str(inner / "sub")) is None


def test_a_workspace_above_the_git_root_is_not_found(outer_and_inner):
    outer, inner = outer_and_inner
    (outer / ".rlsbl-monorepo").mkdir()
    (outer / ".rlsbl-monorepo" / "workspace.toml").write_text("")
    assert find_workspace_root(str(inner)) is None
    assert find_project_root(str(inner)) is None


def test_markers_inside_the_repository_are_still_found(outer_and_inner):
    outer, inner = outer_and_inner
    (outer / ".rlsbl").mkdir()
    deep = outer / "pkg" / "src"
    deep.mkdir(parents=True)
    assert find_project_root(str(deep)) == str(outer.resolve())
    (inner / ".rlsbl").mkdir()
    assert find_project_root(str(inner)) == str(inner.resolve())


def test_a_git_file_marks_a_repository_root_too(tmp_path):
    """A worktree or submodule carries `.git` as a file; it is a root all the same."""
    outer = tmp_path / "outer"
    inner = outer / "vendored"
    inner.mkdir(parents=True)
    (outer / ".rlsbl").mkdir()
    (inner / ".git").write_text("gitdir: /elsewhere\n")
    assert find_project_root(str(inner)) is None


def test_monorepo_init_in_a_nested_repository_is_not_refused(
    outer_and_inner, monkeypatch,
):
    import rlsbl

    outer, inner = outer_and_inner
    (outer / ".rlsbl").mkdir()
    monkeypatch.chdir(inner)
    result = rlsbl.app.test(["monorepo", "init", "--root-dev-node", "--no-auto-commit"])
    assert "inside existing project" not in result.stderr
    assert (inner / ".rlsbl-monorepo" / "workspace.toml").is_file()


def test_a_git_directory_that_is_no_repository_is_walked_past(tmp_path):
    """git itself walks past a `.git` directory with no HEAD, and so does rlsbl."""
    outer = tmp_path / "outer"
    inner = outer / "pkg"
    (inner / ".git").mkdir(parents=True)
    (inner / ".git" / "config").write_text("fake")
    (outer / ".rlsbl").mkdir()
    assert find_project_root(str(inner)) == str(outer.resolve())
