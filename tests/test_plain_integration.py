"""Integration tests for PlainTarget with monorepo commands.

Verifies that plain projects (no-build-system, opt-in via --target plain)
work correctly in the monorepo workflow: sync skips CI/publish generation,
status shows them, release-order includes them, and the dependency graph
handles them as valid nodes.
"""

import json
import os
import subprocess
import textwrap

import pytest

from rlsbl.commands.monorepo import (
    _cmd_init,
    _cmd_add,
    _cmd_sync,
    _cmd_status,
)
from rlsbl.workspace import load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE
from rlsbl.workspace_graph import WorkspaceGraph


# -- Helpers ------------------------------------------------------------------

CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""

PUBLISH_WORKFLOW = """\
name: Publish

on:
  release:
    types: [published]

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo publish
"""


def _make_npm_project(base_path, subdir, version="0.1.0", ci=True, publish=False):
    """Create a minimal npm project with optional CI and publish workflows."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": subdir, "version": version}, f)

    if ci:
        wf_dir = os.path.join(proj_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
            f.write(CI_WORKFLOW)

    if publish:
        wf_dir = os.path.join(proj_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(PUBLISH_WORKFLOW)

    return subdir


def _make_plain_project(base_path, subdir, version="0.1.0"):
    """Create a minimal plain project with a VERSION file and no workflows.

    Also creates .rlsbl/config.json with targets: ["plain"] to match what
    scaffold would produce. In tests the scaffold subprocess fails (no
    installed rlsbl module), so we set it up manually.
    """
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "VERSION"), "w") as f:
        f.write(version + "\n")
    # Create .rlsbl/config.json so detect_targets() finds the plain target
    rlsbl_dir = os.path.join(proj_dir, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
        json.dump({"targets": ["plain"]}, f)
    return subdir


def _init_workspace(base_path, projects):
    """Initialize a workspace and save the given project list directly."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


def _commit_all(base_path, message="setup"):
    """Stage and commit everything in the repo."""
    subprocess.run(["git", "add", "."], cwd=str(base_path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(base_path), check=True,
    )


# -- Test: Sync skips plain projects -----------------------------------------

class TestSyncSkipsPlain:
    """Sync should not generate CI or publish workflows for plain projects,
    and plain projects should not appear in CI router or publish router."""

    def _setup_mixed_workspace(self, mock_git_repo):
        """Set up a workspace with one npm project and one plain project."""
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "web-app", ci=True, publish=True)
        _make_plain_project(mock_git_repo, "shared-config", version="1.0.0")

        # Add projects via _cmd_add
        _cmd_add(["web-app"], {}, project_root=".")
        _cmd_add(["shared-config"], {"target": "plain"}, project_root=".")
        _commit_all(mock_git_repo, "setup mixed workspace")

    def test_no_ci_workflow_for_plain(self, mock_git_repo, capsys):
        """Plain project should have no generated CI workflow at root."""
        self._setup_mixed_workspace(mock_git_repo)
        capsys.readouterr()
        from unittest.mock import patch
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        dest = mock_git_repo / ".github" / "workflows" / "shared-config-ci.yml"
        assert not dest.exists(), "Plain project should not have a generated CI workflow"

    def test_no_publish_workflow_for_plain(self, mock_git_repo, capsys):
        """Plain project should have no generated publish workflow at root."""
        self._setup_mixed_workspace(mock_git_repo)
        capsys.readouterr()
        from unittest.mock import patch
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        dest = mock_git_repo / ".github" / "workflows" / "shared-config-publish.yml"
        assert not dest.exists(), "Plain project should not have a generated publish workflow"

    def test_npm_workflows_generated_normally(self, mock_git_repo, capsys):
        """The npm project should still get its CI workflow and appear in the inline publish router."""
        self._setup_mixed_workspace(mock_git_repo)
        capsys.readouterr()
        from unittest.mock import patch
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        ci_dest = mock_git_repo / ".github" / "workflows" / "web-app-ci.yml"
        assert ci_dest.exists(), "npm project should have a generated CI workflow"
        # Per-project publish wrappers are no longer generated; publish jobs are
        # inlined in publish.yml
        pub_wrapper = mock_git_repo / ".github" / "workflows" / "web-app-publish.yml"
        assert not pub_wrapper.exists(), "Per-project publish wrappers should not be generated"
        router = mock_git_repo / ".github" / "workflows" / "publish.yml"
        assert router.exists(), "Inline publish router should be generated"
        content = router.read_text()
        assert "web-app-" in content, "npm project jobs should be inlined in publish router"

    def test_plain_not_in_ci_router(self, mock_git_repo, capsys):
        """Plain project should not appear in the CI router."""
        self._setup_mixed_workspace(mock_git_repo)
        capsys.readouterr()
        from unittest.mock import patch
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        router = mock_git_repo / ".github" / "workflows" / "ci-router.yml"
        assert router.exists(), "CI router should be generated"
        content = router.read_text()
        assert "shared-config" not in content, (
            "Plain project should not appear in CI router"
        )
        # npm project should be in the router
        assert "web-app" in content

    def test_plain_not_in_publish_router(self, mock_git_repo, capsys):
        """Plain project should not appear in the publish router."""
        self._setup_mixed_workspace(mock_git_repo)
        capsys.readouterr()
        from unittest.mock import patch
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        router = mock_git_repo / ".github" / "workflows" / "publish.yml"
        assert router.exists(), "Publish router should be generated (npm has publish)"
        content = router.read_text()
        assert "shared-config" not in content, (
            "Plain project should not appear in publish router"
        )
        assert "web-app" in content


# -- Test: Status shows plain projects ---------------------------------------

class TestStatusShowsPlain:
    """Monorepo status should display plain projects with Target='plain'
    and their VERSION file version."""

    def test_status_shows_plain_project(self, mock_git_repo, capsys):
        """Plain project appears in status output with correct target and version."""
        _cmd_init({}, project_root=".")
        _make_plain_project(mock_git_repo, "my-config", version="1.2.3")
        _cmd_add(["my-config"], {"target": "plain"}, project_root=".")
        capsys.readouterr()

        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()

        # Header columns
        assert "Project" in captured.out
        assert "Target" in captured.out
        assert "Version" in captured.out

        # Project row
        assert "my-config" in captured.out
        assert "plain" in captured.out
        assert "1.2.3" in captured.out

    def test_status_plain_and_npm_mixed(self, mock_git_repo, capsys):
        """Both plain and npm projects show up in status with correct targets."""
        _cmd_init({}, project_root=".")
        _make_npm_project(mock_git_repo, "webapp", version="2.0.0", ci=False)
        _make_plain_project(mock_git_repo, "specs", version="0.5.0")
        _cmd_add(["webapp"], {}, project_root=".")
        _cmd_add(["specs"], {"target": "plain"}, project_root=".")
        capsys.readouterr()

        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()

        lines = captured.out.strip().split("\n")
        # Header + 2 project rows
        assert len(lines) == 3

        # Find each project's line
        webapp_line = [l for l in lines if "webapp" in l][0]
        specs_line = [l for l in lines if "specs" in l][0]

        assert "npm" in webapp_line
        assert "2.0.0" in webapp_line
        assert "plain" in specs_line
        assert "0.5.0" in specs_line


# -- Test: Release-order includes plain projects -----------------------------

class TestReleaseOrderIncludesPlain:
    """Release-order should include plain projects in the topological sort,
    respecting depends_on edges."""

    def test_plain_before_dependent(self, mock_git_repo, capsys):
        """An npm project that depends_on a plain project: plain comes first."""
        _make_plain_project(mock_git_repo, "infra", version="1.0.0")
        _make_npm_project(mock_git_repo, "app", version="1.0.0", ci=False)

        projects = [
            {"path": "infra", "name": "infra", "depends_on": []},
            {"path": "app", "name": "app", "depends_on": ["infra"]},
        ]
        _init_workspace(mock_git_repo, projects)

        graph = WorkspaceGraph(str(mock_git_repo), projects)
        order = graph.topological_order()

        assert "infra" in order
        assert "app" in order
        assert order.index("infra") < order.index("app"), (
            "Plain project (infra) should come before its dependent (app)"
        )

    def test_plain_as_leaf_in_chain(self, mock_git_repo, capsys):
        """A plain project at the root of a dep chain appears first."""
        _make_plain_project(mock_git_repo, "config", version="0.1.0")
        _make_npm_project(mock_git_repo, "lib", version="0.1.0", ci=False)
        _make_npm_project(mock_git_repo, "app", version="0.1.0", ci=False)

        projects = [
            {"path": "config", "name": "config"},
            {"path": "lib", "name": "lib", "depends_on": ["config"]},
            {"path": "app", "name": "app", "depends_on": ["lib"]},
        ]
        _init_workspace(mock_git_repo, projects)

        graph = WorkspaceGraph(str(mock_git_repo), projects)
        order = graph.topological_order()

        assert order == ["config", "lib", "app"]


# -- Test: Plain project in dependency graph ----------------------------------

class TestPlainInDependencyGraph:
    """Plain projects can be dependency targets; dep_count and rdep_count
    work correctly with them."""

    def test_plain_as_dependency_target(self, tmp_path):
        """A plain project can be listed in another project's depends_on."""
        projects = [
            {"path": "shared", "name": "shared"},
            {"path": "app", "name": "app", "depends_on": ["shared"]},
        ]
        # Create project dirs
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "VERSION").write_text("1.0.0\n")
        (tmp_path / "app").mkdir()

        graph = WorkspaceGraph(str(tmp_path), projects)

        deps = graph.dependencies("app")
        assert len(deps) == 1
        assert deps[0].name == "shared"
        assert deps[0].dep_type == "explicit"

    def test_dep_count_with_plain(self, tmp_path):
        """dep_count is correct when a plain project has dependents."""
        projects = [
            {"path": "infra", "name": "infra"},
            {"path": "svc-a", "name": "svc-a", "depends_on": ["infra"]},
            {"path": "svc-b", "name": "svc-b", "depends_on": ["infra"]},
        ]
        for p in projects:
            d = tmp_path / p["path"]
            d.mkdir()
            (d / "VERSION").write_text("0.1.0\n")

        graph = WorkspaceGraph(str(tmp_path), projects)

        # infra: 0 deps, 2 rdeps
        assert graph.dep_count("infra") == 0
        assert graph.rdep_count("infra") == 2

        # svc-a, svc-b: 1 dep each, 0 rdeps
        assert graph.dep_count("svc-a") == 1
        assert graph.rdep_count("svc-a") == 0
        assert graph.dep_count("svc-b") == 1
        assert graph.rdep_count("svc-b") == 0

    def test_rdep_count_with_plain(self, tmp_path):
        """rdep_count on a plain project counts all dependents."""
        projects = [
            {"path": "base", "name": "base"},
            {"path": "mid", "name": "mid", "depends_on": ["base"]},
            {"path": "top", "name": "top", "depends_on": ["mid", "base"]},
        ]
        for p in projects:
            (tmp_path / p["path"]).mkdir()

        graph = WorkspaceGraph(str(tmp_path), projects)

        assert graph.rdep_count("base") == 2  # mid and top depend on base
        assert graph.rdep_count("mid") == 1   # only top depends on mid
        assert graph.rdep_count("top") == 0   # nothing depends on top

    def test_plain_and_npm_mixed_graph(self, tmp_path):
        """Mixed plain + npm projects with both explicit and auto-detected deps."""
        projects = [
            {"path": "infra", "name": "infra"},               # plain
            {"path": "core", "name": "core"},                  # npm
            {"path": "app", "name": "app", "depends_on": ["infra"]},  # npm, explicit dep on plain
        ]
        (tmp_path / "infra").mkdir()
        (tmp_path / "infra" / "VERSION").write_text("1.0.0\n")

        # core: npm project with no deps
        core_dir = tmp_path / "core"
        core_dir.mkdir()
        (core_dir / "package.json").write_text(
            json.dumps({"name": "core", "version": "1.0.0"})
        )

        # app: npm project that also depends on core via package.json
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            json.dumps({
                "name": "app",
                "version": "1.0.0",
                "dependencies": {"core": "^1.0.0"},
            })
        )

        graph = WorkspaceGraph(str(tmp_path), projects)

        # app should depend on both infra (explicit) and core (auto-detected)
        app_deps = graph.dependencies("app")
        dep_names = {d.name for d in app_deps}
        assert dep_names == {"infra", "core"}

        # Verify dep types
        dep_by_name = {d.name: d for d in app_deps}
        assert dep_by_name["infra"].dep_type == "explicit"
        assert dep_by_name["core"].dep_type == "versioned"

        # Counts
        assert graph.dep_count("app") == 2
        assert graph.rdep_count("infra") == 1
        assert graph.rdep_count("core") == 1
