"""Tests for the check context types used by the strictcli check system."""

from pathlib import Path

from rlsbl.check_context import ProjectCheckContext, WorkspaceCheckContext


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
