"""Tests for the scope adapter and scope-based check filtering."""

import os
from pathlib import Path

from strictcli import SkipCheck

from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.checks.scope import scope_adapter
from rlsbl.context import ProjectContext


def _make_ws_ctx(projects=None, push_stdin=None, workspace_root=None):
    """Create a minimal WorkspaceCheckContext for tests."""
    return WorkspaceCheckContext(
        project_root=Path("/tmp/proj"),
        workspace_root=workspace_root or Path("/tmp/ws"),
        config={},
        projects=projects or [],
        graph=None,
        push_stdin=push_stdin,
    )


def _make_proj_ctx(push_stdin=None):
    """Create a minimal ProjectContext (non-workspace) for tests."""
    return ProjectContext(
        project_root=Path("/tmp/proj"),
        workspace_root=None,
        config={},
        push_stdin=push_stdin,
    )


# ---------------------------------------------------------------------------
# Token: workspace
# ---------------------------------------------------------------------------


def test_workspace_token_passes_workspace_ctx():
    ctx = _make_ws_ctx(projects=[{"name": "a", "path": "a"}])
    result = scope_adapter(ctx, "workspace")
    assert isinstance(result, WorkspaceCheckContext)
    assert result is ctx


def test_workspace_token_skips_non_workspace_ctx():
    ctx = _make_proj_ctx()
    result = scope_adapter(ctx, "workspace")
    assert isinstance(result, SkipCheck)
    assert "not a monorepo workspace" in result.reason


# ---------------------------------------------------------------------------
# Token: non_dev_only
# ---------------------------------------------------------------------------


def test_non_dev_only_filters_dev_only_projects():
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "devtool", "path": "devtool", "dev_only": True},
        {"name": "app", "path": "app"},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "non_dev_only")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 2
    names = {p["name"] for p in result.projects}
    assert names == {"lib", "app"}


def test_non_dev_only_filters_dev_node_projects():
    """dev_node is treated as dev_only by project_is_dev_only."""
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "tests", "path": "tests", "dev_node": True},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "non_dev_only")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 1
    assert result.projects[0]["name"] == "lib"


def test_non_dev_only_does_not_mutate_original():
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "devtool", "path": "devtool", "dev_only": True},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "non_dev_only")
    assert len(ctx.projects) == 2  # original unchanged
    assert len(result.projects) == 1


def test_non_dev_only_passthrough_for_non_workspace():
    ctx = _make_proj_ctx()
    result = scope_adapter(ctx, "non_dev_only")
    assert result is ctx  # unchanged


# ---------------------------------------------------------------------------
# Token: non_dev_node
# ---------------------------------------------------------------------------


def test_non_dev_node_filters_dev_node_projects():
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "tests", "path": "tests", "dev_node": True},
        {"name": "app", "path": "app"},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "non_dev_node")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 2
    names = {p["name"] for p in result.projects}
    assert names == {"lib", "app"}


# ---------------------------------------------------------------------------
# Token: library
# ---------------------------------------------------------------------------


def test_library_filters_to_library_projects():
    projects = [
        {"name": "core", "path": "core", "library": True},
        {"name": "app", "path": "app"},
        {"name": "utils", "path": "utils", "library": True},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "library")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 2
    names = {p["name"] for p in result.projects}
    assert names == {"core", "utils"}


def test_library_empty_when_no_libraries():
    projects = [
        {"name": "app", "path": "app"},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "library")
    assert isinstance(result, WorkspaceCheckContext)
    assert result.projects == []


# ---------------------------------------------------------------------------
# Token: releasable
# ---------------------------------------------------------------------------


def test_releasable_filters_to_releasable_projects():
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "tests", "path": "tests", "dev_node": True},
        {"name": "app", "path": "app", "releasable": False},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "releasable")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 1
    assert result.projects[0]["name"] == "lib"


# ---------------------------------------------------------------------------
# Token: push
# ---------------------------------------------------------------------------


def test_push_token_passes_when_push_stdin_present():
    ctx = _make_ws_ctx(push_stdin="refs/heads/main old new\n")
    result = scope_adapter(ctx, "push")
    assert isinstance(result, WorkspaceCheckContext)
    assert result is ctx


def test_push_token_skips_when_push_stdin_none():
    ctx = _make_ws_ctx(push_stdin=None)
    result = scope_adapter(ctx, "push")
    assert isinstance(result, SkipCheck)
    assert "not in push context" in result.reason


def test_push_token_skips_on_project_ctx():
    ctx = _make_proj_ctx(push_stdin=None)
    result = scope_adapter(ctx, "push")
    assert isinstance(result, SkipCheck)


# ---------------------------------------------------------------------------
# Token composition
# ---------------------------------------------------------------------------


def test_workspace_non_dev_only_composition():
    projects = [
        {"name": "lib", "path": "lib"},
        {"name": "devtool", "path": "devtool", "dev_only": True},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "workspace:non_dev_only")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 1
    assert result.projects[0]["name"] == "lib"


def test_workspace_library_composition():
    projects = [
        {"name": "core", "path": "core", "library": True},
        {"name": "app", "path": "app"},
    ]
    ctx = _make_ws_ctx(projects=projects)
    result = scope_adapter(ctx, "workspace:library")
    assert isinstance(result, WorkspaceCheckContext)
    assert len(result.projects) == 1
    assert result.projects[0]["name"] == "core"


def test_workspace_non_dev_only_skips_non_workspace():
    """workspace:non_dev_only should skip on non-workspace context."""
    ctx = _make_proj_ctx()
    result = scope_adapter(ctx, "workspace:non_dev_only")
    assert isinstance(result, SkipCheck)


def test_composition_short_circuits_on_skip_check():
    """If workspace token returns SkipCheck, non_dev_only never runs."""
    ctx = _make_proj_ctx()
    result = scope_adapter(ctx, "workspace:non_dev_only")
    assert isinstance(result, SkipCheck)
    assert "not a monorepo workspace" in result.reason


# ---------------------------------------------------------------------------
# Unknown tokens
# ---------------------------------------------------------------------------


def test_unknown_token_passes_through():
    ctx = _make_ws_ctx(projects=[{"name": "a", "path": "a"}])
    result = scope_adapter(ctx, "unknown_token")
    assert result is ctx


def test_unknown_token_in_composition():
    ctx = _make_ws_ctx(projects=[{"name": "a", "path": "a"}])
    result = scope_adapter(ctx, "workspace:unknown_token")
    assert isinstance(result, WorkspaceCheckContext)
    assert result is ctx  # workspace passes, unknown passes through


# ---------------------------------------------------------------------------
# scaffold-gitignore-stale check
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# workspace-ci-synced + non_dev_node scope integration
# ---------------------------------------------------------------------------


def _register_and_get_check(name):
    """Register workspace checks on a mock app and return the named check fn.

    The returned function accepts only ``ctx`` and internally creates the
    appropriate reporter (ErrorReporter for error_check, WarnReporter for
    warn_check), matching what strictcli's ``_CheckDef.impl`` does.
    """
    from unittest.mock import MagicMock

    from strictcli import ErrorReporter, WarnReporter
    from rlsbl.checks.workspace import register_workspace_checks

    mock_app = MagicMock()
    checks = {}

    def _make_capture(reporter_cls):
        def capture_check(check_name):
            def decorator(fn):
                def run(ctx):
                    return fn(ctx, reporter_cls())
                checks[check_name] = run
                return fn
            return decorator
        return capture_check

    mock_app.error_check = _make_capture(ErrorReporter)
    mock_app.warn_check = _make_capture(WarnReporter)
    register_workspace_checks(mock_app)
    return checks[name]


def test_ci_synced_skips_dev_node_via_scope(tmp_path):
    """A dev_node project with no CI workflow is filtered out by the
    workspace:non_dev_node scope, so workspace-ci-synced passes."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".github" / "workflows").mkdir(parents=True)

    projects = [
        {"name": "devtool", "path": "devtool", "dev_node": True},
    ]
    ctx = _make_ws_ctx(projects=projects, workspace_root=ws)

    # Apply the scope adapter (simulates what strictcli does before calling the check)
    filtered = scope_adapter(ctx, "workspace:non_dev_node")
    assert isinstance(filtered, WorkspaceCheckContext)
    assert len(filtered.projects) == 0  # dev_node filtered out

    check_fn = _register_and_get_check("workspace-ci-synced")
    result = check_fn(filtered)
    assert result.status == "pass"


def test_ci_synced_fails_non_dev_node_missing_workflow(tmp_path):
    """A non-dev-node project whose jobs are absent from ci-router.yml should
    still fail workspace-ci-synced even after the scope adapter runs."""
    ws = tmp_path / "ws"
    ws.mkdir()
    wf = ws / ".github" / "workflows"
    wf.mkdir(parents=True)
    # Router exists but has no jobs for mylib.
    (wf / "ci-router.yml").write_text(
        "name: CI Router\non: push\njobs:\n  detect:\n    runs-on: ubuntu-latest\n"
    )

    projects = [
        {"name": "mylib", "path": "mylib"},
    ]
    ctx = _make_ws_ctx(projects=projects, workspace_root=ws)

    # Apply the scope adapter -- non-dev-node project is NOT filtered out
    filtered = scope_adapter(ctx, "workspace:non_dev_node")
    assert isinstance(filtered, WorkspaceCheckContext)
    assert len(filtered.projects) == 1

    check_fn = _register_and_get_check("workspace-ci-synced")
    result = check_fn(filtered)
    assert result.status == "fail"
    assert "mylib" in result.message


def test_scaffold_gitignore_stale_passes_when_entries_present(tmp_path):
    """Check passes when all rlsbl-specific entries are in .gitignore."""
    from importlib.resources import files as pkg_files

    # Read the actual rlsbl entries from the template
    template_text = (
        pkg_files("rlsbl") / "templates" / "shared" / "gitignore.tpl"
    ).read_text()
    rlsbl_entries = [
        line.strip()
        for line in template_text.splitlines()
        if ".rlsbl" in line and line.strip() and not line.strip().startswith("#")
    ]

    # Create a project directory with a .gitignore containing all entries
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    gitignore_content = "\n".join(rlsbl_entries) + "\n"
    (proj_dir / ".gitignore").write_text(gitignore_content)

    projects = [{"name": "proj", "path": "proj"}]
    ctx = _make_ws_ctx(projects=projects, workspace_root=tmp_path)

    check_fn = _register_and_get_check("scaffold-gitignore-stale")
    result = check_fn(ctx)
    assert result.status == "pass"


def test_scaffold_gitignore_stale_warns_on_missing_entries(tmp_path):
    """Check warns when rlsbl-specific entries are missing from .gitignore."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    # Write a .gitignore without rlsbl entries
    (proj_dir / ".gitignore").write_text("node_modules/\n__pycache__/\n")

    projects = [{"name": "proj", "path": "proj"}]
    ctx = _make_ws_ctx(projects=projects, workspace_root=tmp_path)

    check_fn = _register_and_get_check("scaffold-gitignore-stale")
    result = check_fn(ctx)
    assert result.status == "warn"
    assert len(result.problems) == 1
    assert "proj" in result.problems[0].text
    assert ".rlsbl" in result.problems[0].text


def test_scaffold_gitignore_stale_warns_on_missing_gitignore(tmp_path):
    """Check warns when .gitignore doesn't exist at all."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    projects = [{"name": "proj", "path": "proj"}]
    ctx = _make_ws_ctx(projects=projects, workspace_root=tmp_path)

    check_fn = _register_and_get_check("scaffold-gitignore-stale")
    result = check_fn(ctx)
    assert result.status == "warn"
    assert ".gitignore not found" in result.problems[0].text


def test_scaffold_gitignore_stale_multiple_projects(tmp_path):
    """Check reports per-project details for multiple projects."""
    # Project A: has correct gitignore (all rlsbl entries)
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    (proj_a / ".gitignore").write_text(
        ".rlsbl-notes-*.tmp\n.rlsbl/lock\n.rlsbl-monorepo/lock\n"
    )

    # Project B: missing entries
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / ".gitignore").write_text("*.pyc\n")

    projects = [
        {"name": "proj_a", "path": "proj_a"},
        {"name": "proj_b", "path": "proj_b"},
    ]
    ctx = _make_ws_ctx(projects=projects, workspace_root=tmp_path)

    check_fn = _register_and_get_check("scaffold-gitignore-stale")
    result = check_fn(ctx)
    assert result.status == "warn"
    # Only proj_b should be in the problems
    proj_names_in_problems = [p.text.split(":")[0] for p in result.problems]
    assert "proj_b" in proj_names_in_problems
    assert "proj_a" not in proj_names_in_problems
