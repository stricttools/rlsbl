"""Tests for the check context types used by the strictcli check system."""

import json
from pathlib import Path

from rlsbl.context import ProjectContext
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace_graph import WorkspaceGraph

from conftest import make_workspace, run_git


def test_project_context_has_project_root():
    ctx = ProjectContext(project_root=Path("/tmp/proj"), workspace_root=None, config={})
    assert ctx.project_root == Path("/tmp/proj")


def test_workspace_check_context_has_all_attributes():
    ctx = WorkspaceCheckContext(
        project_root=Path("/tmp/proj"),
        workspace_root=Path("/tmp/ws"),
        config={},
        projects=[{"path": "a", "name": "a"}],
        graph=object(),
    )
    assert ctx.workspace_root == Path("/tmp/ws")
    assert ctx.projects == [{"path": "a", "name": "a"}]
    assert ctx.graph is not None


def test_workspace_check_context_is_subclass_of_project():
    assert issubclass(WorkspaceCheckContext, ProjectContext)
    ctx = WorkspaceCheckContext(
        project_root=Path("/tmp/proj"),
        workspace_root=Path("/tmp/ws"),
        config={},
        projects=[],
        graph=None,
    )
    assert isinstance(ctx, ProjectContext)


def test_check_context_factory_passes_workspace_root(tmp_path, monkeypatch):
    """Regression: _check_context_factory must pass workspace_root to WorkspaceGraph.

    Previously the call was WorkspaceGraph(projects) which raised TypeError
    because WorkspaceGraph.__init__ requires (root, projects).
    """
    ws_dir = tmp_path / ".rlsbl-monorepo"
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(
        '[[releasables]]\nname = "subproj"\n\n'
        '[[projects]]\npath = "sub"\nname = "subproj"\nreleasable = "subproj"\n'
    )

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


def test_get_changelog_context_uses_target_specific_tag_glob(tmp_path, monkeypatch):
    """_get_changelog_context uses the target's monorepo_tag_glob for Go sub-projects.

    Go targets use 'path/v*' format, not 'name@v*'. This test verifies that
    the check system calls monorepo_tag_glob() instead of hardcoding the format.
    """
    monkeypatch.chdir(tmp_path)

    # Initialize git repo
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    # Initial commit
    (tmp_path / "README.md").write_text("# test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    # Set up monorepo workspace with a Go sub-project
    projects = [{"path": "go", "name": "mygolib"}]
    make_workspace(tmp_path, projects)

    go_dir = tmp_path / "go"
    go_dir.mkdir()

    # Create go.mod so detect_targets identifies it as a Go target
    (go_dir / "go.mod").write_text("module github.com/example/mygolib\n\ngo 1.21\n")
    (go_dir / "VERSION").write_text("0.1.0\n")

    # Create .rlsbl/changes/ with an empty unreleased.jsonl
    (go_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (go_dir / ".rlsbl" / "config.json").write_text(json.dumps({"private": False}) + "\n")

    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-q", "-m", "add go project")

    # Build a WorkspaceCheckContext pointing at the Go sub-project
    ctx = WorkspaceCheckContext(
        project_root=go_dir.resolve(),
        workspace_root=tmp_path.resolve(),
        config={},
        projects=projects,
        graph=None,
    )

    # Import and call _get_changelog_context via the register_checks closure
    # We need to access the inner function, so we import and call it through
    # the checks module directly.
    from rlsbl.checks import register_checks
    from unittest.mock import MagicMock

    app = MagicMock()
    app._checks_enabled = True
    # Collect the _get_changelog_context function via the check registrations
    check_fns = {}
    def fake_check(name):
        def decorator(fn):
            check_fns[name] = fn
            return fn
        return decorator
    app.check = fake_check
    register_checks(app)

    # Call any changelog check that uses _get_changelog_context -- changelog-hashes
    # is simplest. But we need the inner helper directly. Since it's a closure,
    # we access it through a changelog check function.
    # Instead, let's directly test by calling the check and inspecting.
    # The easiest approach: patch check_in_range to capture the tag_glob argument.
    from unittest.mock import patch
    captured = {}

    def capture_check_in_range(entries, tag_glob, project=None):
        captured["tag_glob"] = tag_glob
        captured["project"] = project
        return True, []

    monkeypatch.chdir(go_dir)
    with patch("rlsbl.changelog.validate.check_in_range", capture_check_in_range):
        result = check_fns["changelog-range"](ctx)

    # Go target should produce "go/v*" not "mygolib@v*"
    assert captured["tag_glob"] == "go/v*", (
        f"Expected Go-style tag glob 'go/v*', got '{captured['tag_glob']}'"
    )
