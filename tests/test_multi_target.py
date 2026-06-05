"""Tests for multi-target handling: snapshot, graph, and status correctly expose all targets."""

import json
import os

import pytest

from rlsbl.commands.monorepo import _cmd_graph, _cmd_status
from rlsbl.snapshot import generate_snapshot
from rlsbl.workspace import save_workspace, WORKSPACE_DIR
from rlsbl.workspace_graph import WorkspaceGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_multi_target_project(base_path, subdir, version="0.1.0"):
    """Create a project with both pypi and npm targets (pyproject.toml + package.json)."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)

    # pyproject.toml for pypi target
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(f'[project]\nname = "{subdir}"\nversion = "{version}"\n')

    # package.json for npm target
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": subdir, "version": version}, f)


def _make_single_target_project(base_path, subdir, target="npm", version="0.1.0"):
    """Create a project with a single target."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)

    if target == "npm":
        with open(os.path.join(proj_dir, "package.json"), "w") as f:
            json.dump({"name": subdir, "version": version}, f)
    elif target == "pypi":
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write(f'[project]\nname = "{subdir}"\nversion = "{version}"\n')


def _init_workspace(base_path, projects):
    """Initialize a workspace with the given project list."""
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


def _make_workspace(tmp_path, project_defs):
    """Create workspace dirs and project manifests for generate_snapshot tests.

    project_defs is a list of dicts with: name, path, version,
    and optionally targets (list of target names to create manifests for).
    """
    root = str(tmp_path)
    projects = []

    for pdef in project_defs:
        proj_dir = tmp_path / pdef["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)

        proj = {"path": pdef["path"], "name": pdef["name"]}
        projects.append(proj)

        version = pdef.get("version", "0.1.0")
        targets = pdef.get("targets", ["pypi"])

        for t in targets:
            if t == "pypi":
                (proj_dir / "pyproject.toml").write_text(
                    f'[project]\nname = "{pdef["name"]}"\nversion = "{version}"\n'
                )
            elif t == "npm":
                (proj_dir / "package.json").write_text(
                    json.dumps({"name": pdef["name"], "version": version})
                )

    return root, projects


# ---------------------------------------------------------------------------
# Snapshot: multi-target
# ---------------------------------------------------------------------------


class TestSnapshotMultiTarget:
    """Verify snapshot stores all targets for multi-target projects."""

    def test_snapshot_contains_both_targets(self, tmp_path):
        """A project with pypi + npm targets should have both in snapshot."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "dual", "path": "packages/dual", "version": "1.0.0",
             "targets": ["pypi", "npm"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        pkg = snapshot["packages"]["dual"]
        assert "targets" in pkg, "snapshot should use 'targets' (plural) key"
        assert isinstance(pkg["targets"], list)
        assert "pypi" in pkg["targets"]
        assert "npm" in pkg["targets"]
        assert len(pkg["targets"]) == 2

    def test_snapshot_single_target_is_list(self, tmp_path):
        """A single-target project should still have a list in 'targets'."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "solo", "path": "packages/solo", "version": "1.0.0",
             "targets": ["npm"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        pkg = snapshot["packages"]["solo"]
        assert pkg["targets"] == ["npm"]

    def test_snapshot_no_target_is_empty_list(self, tmp_path):
        """A project with no detectable targets should have an empty list."""
        root = str(tmp_path)
        proj_dir = tmp_path / "packages" / "bare"
        proj_dir.mkdir(parents=True)
        # No manifest files -- detect_targets returns []

        projects = [{"path": "packages/bare", "name": "bare"}]
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        pkg = snapshot["packages"]["bare"]
        assert pkg["targets"] == []

    def test_snapshot_version_from_first_target(self, tmp_path):
        """Version should be read from the first detected target."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "dual", "path": "packages/dual", "version": "2.5.0",
             "targets": ["pypi", "npm"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        pkg = snapshot["packages"]["dual"]
        assert pkg["version"] == "2.5.0"

    def test_snapshot_mixed_single_and_multi(self, tmp_path):
        """Workspace with both single-target and multi-target projects."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "multi", "path": "packages/multi", "version": "1.0.0",
             "targets": ["pypi", "npm"]},
            {"name": "single", "path": "packages/single", "version": "2.0.0",
             "targets": ["npm"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        assert len(snapshot["packages"]["multi"]["targets"]) == 2
        assert len(snapshot["packages"]["single"]["targets"]) == 1


# ---------------------------------------------------------------------------
# Graph: multi-target
# ---------------------------------------------------------------------------


class TestGraphMultiTarget:
    """Verify graph output includes all targets for multi-target projects."""

    def test_graph_json_contains_both_targets(self, mock_git_repo, capsys):
        """JSON graph output should list all targets for a multi-target project."""
        _make_multi_target_project(mock_git_repo, "dual", version="1.0.0")
        _make_single_target_project(mock_git_repo, "single", target="npm", version="2.0.0")

        projects = [
            {"path": "dual", "name": "dual"},
            {"path": "single", "name": "single"},
        ]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        dual = data["packages"]["dual"]
        assert "targets" in dual, "graph should use 'targets' (plural) key"
        assert isinstance(dual["targets"], list)
        assert "pypi" in dual["targets"]
        assert "npm" in dual["targets"]

        single = data["packages"]["single"]
        assert single["targets"] == ["npm"]

    def test_graph_multi_target_version_readable(self, mock_git_repo, capsys):
        """Version should still be readable for multi-target projects."""
        _make_multi_target_project(mock_git_repo, "dual", version="3.1.0")

        projects = [{"path": "dual", "name": "dual"}]
        _init_workspace(mock_git_repo, projects)

        _cmd_graph({"format": "json"}, project_root=".")
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["packages"]["dual"]["version"] == "3.1.0"


# ---------------------------------------------------------------------------
# Status: multi-target
# ---------------------------------------------------------------------------


class TestStatusMultiTarget:
    """Verify monorepo status shows all targets for multi-target projects."""

    def test_status_shows_comma_separated_targets(self, mock_git_repo, capsys):
        """Status should show comma-separated targets for multi-target projects."""
        from rlsbl.commands.monorepo import _cmd_init

        _cmd_init({}, project_root=".")
        _make_multi_target_project(mock_git_repo, "dual", version="1.0.0")
        capsys.readouterr()

        # Manually add to workspace (bypassing _cmd_add to avoid scaffold)
        projects = [{"path": "dual", "name": "dual"}]
        save_workspace(str(mock_git_repo), projects)

        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()

        # The Target column should contain both targets
        lines = captured.out.strip().split("\n")
        assert len(lines) >= 2  # header + at least one data row
        data_line = lines[1]
        # Both target names should appear in the row (comma-separated)
        assert "pypi" in data_line
        assert "npm" in data_line

    def test_status_single_target_unchanged(self, mock_git_repo, capsys):
        """Single-target projects should display normally in status."""
        from rlsbl.commands.monorepo import _cmd_init

        _cmd_init({}, project_root=".")
        _make_single_target_project(mock_git_repo, "solo", target="npm", version="1.0.0")
        capsys.readouterr()

        projects = [{"path": "solo", "name": "solo"}]
        save_workspace(str(mock_git_repo), projects)

        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()

        lines = captured.out.strip().split("\n")
        data_line = lines[1]
        assert "npm" in data_line
