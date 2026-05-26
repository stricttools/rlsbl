"""Tests for the check context types used by the strictcli check system."""

from pathlib import Path

from rlsbl.check_context import ProjectCheckContext, WorkspaceCheckContext
from rlsbl.workspace_graph import WorkspaceGraph

from conftest import make_workspace


def test_project_check_context_has_project_root():
    ctx = ProjectCheckContext(project_root=Path("/tmp/proj"))
    assert ctx.project_root == Path("/tmp/proj")


def test_workspace_check_context_has_all_attributes():
    ctx = WorkspaceCheckContext(
        project_root=Path("/tmp/proj"),
        workspace_root=Path("/tmp/ws"),
        projects=[{"path": "a", "name": "a"}],
        graph=object(),
    )
    assert ctx.workspace_root == Path("/tmp/ws")
    assert ctx.projects == [{"path": "a", "name": "a"}]
    assert ctx.graph is not None


def test_workspace_check_context_is_subclass_of_project():
    assert issubclass(WorkspaceCheckContext, ProjectCheckContext)
    ctx = WorkspaceCheckContext(
        project_root=Path("/tmp/proj"),
        workspace_root=Path("/tmp/ws"),
        projects=[],
        graph=None,
    )
    assert isinstance(ctx, ProjectCheckContext)


def test_check_context_factory_passes_workspace_root(tmp_path, monkeypatch):
    """Regression: _check_context_factory must pass workspace_root to WorkspaceGraph.

    Previously the call was WorkspaceGraph(projects) which raised TypeError
    because WorkspaceGraph.__init__ requires (root, projects).
    """
    projects = [{"path": "sub", "name": "subproj"}]
    make_workspace(tmp_path, projects)

    # Create the subproject directory so WorkspaceGraph can scan it
    (tmp_path / "sub").mkdir()

    monkeypatch.chdir(tmp_path)

    from rlsbl import _check_context_factory

    ctx = _check_context_factory()

    assert isinstance(ctx, WorkspaceCheckContext)
    assert isinstance(ctx.graph, WorkspaceGraph)
    assert ctx.workspace_root == tmp_path.resolve()
    assert len(ctx.projects) == 1
    assert ctx.projects[0]["name"] == "subproj"
