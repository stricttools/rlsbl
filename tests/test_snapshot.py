"""Tests for rlsbl.snapshot and the monorepo snapshot command."""

import json
import os

import pytest

from rlsbl.snapshot import (
    SNAPSHOT_FILE,
    check_snapshot,
    generate_snapshot,
    write_snapshot,
)
from rlsbl.workspace import WORKSPACE_DIR
from rlsbl.workspace_graph import WorkspaceGraph

from conftest import workspace_toml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path, project_defs):
    """Create workspace dirs and project manifests, return (root, projects).

    project_defs is a list of dicts, each with:
        name, path, target ("pypi" or "npm"), version,
        and optionally description, library, test_only, depends_on.
    """
    root = str(tmp_path)
    projects = []

    for pdef in project_defs:
        proj_dir = tmp_path / pdef["path"]
        proj_dir.mkdir(parents=True, exist_ok=True)

        proj = {
            "path": pdef["path"],
            "name": pdef["name"],
        }
        if "description" in pdef:
            proj["description"] = pdef["description"]
        if pdef.get("library"):
            proj["library"] = True
        if pdef.get("test_only"):
            proj["test_only"] = True
        if "depends_on" in pdef:
            proj["depends_on"] = pdef["depends_on"]
        projects.append(proj)

        # Create manifest so detect_targets works
        target = pdef.get("target", "pypi")
        version = pdef.get("version", "0.1.0")
        if target == "pypi":
            (proj_dir / "pyproject.toml").write_text(
                f'[project]\nname = "{pdef["name"]}"\nversion = "{version}"\n'
            )
        elif target == "npm":
            (proj_dir / "package.json").write_text(
                json.dumps({"name": pdef["name"], "version": version})
            )

    return root, projects


# ---------------------------------------------------------------------------
# generate_snapshot
# ---------------------------------------------------------------------------


class TestGenerateSnapshot:
    """Tests for generate_snapshot() with a 3-package workspace."""

    def test_basic_structure(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "schema", "path": "packages/schema", "target": "pypi", "version": "1.0.0"},
            {"name": "models", "path": "packages/models", "target": "pypi", "version": "2.1.0",
             "description": "Core data models", "library": True, "depends_on": ["schema"]},
            {"name": "app", "path": "packages/app", "target": "npm", "version": "0.5.0",
             "depends_on": ["models"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        assert "generated_at" in snapshot
        assert snapshot["package_count"] == 3
        assert set(snapshot["packages"].keys()) == {"schema", "models", "app"}

    def test_package_fields(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "schema", "path": "packages/schema", "target": "pypi", "version": "1.0.0"},
            {"name": "models", "path": "packages/models", "target": "pypi", "version": "2.1.0",
             "description": "Core data models", "library": True, "depends_on": ["schema"]},
            {"name": "app", "path": "packages/app", "target": "npm", "version": "0.5.0",
             "depends_on": ["models"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        models = snapshot["packages"]["models"]
        assert models["path"] == "packages/models"
        assert models["targets"] == ["pypi"]
        assert models["version"] == "2.1.0"
        assert models["description"] == "Core data models"
        assert models["deps"] == ["schema"]
        assert models["rdeps"] == ["app"]
        assert models["library"] is True
        assert models["test_only"] is False

    def test_leaf_and_root_nodes(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "schema", "path": "packages/schema", "target": "pypi", "version": "1.0.0"},
            {"name": "models", "path": "packages/models", "target": "pypi", "version": "2.1.0",
             "depends_on": ["schema"]},
            {"name": "app", "path": "packages/app", "target": "npm", "version": "0.5.0",
             "depends_on": ["models"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        assert snapshot["graph"]["leaf_nodes"] == ["schema"]
        assert snapshot["graph"]["root_nodes"] == ["app"]

    def test_topological_order(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "schema", "path": "packages/schema", "target": "pypi", "version": "1.0.0"},
            {"name": "models", "path": "packages/models", "target": "pypi", "version": "2.1.0",
             "depends_on": ["schema"]},
            {"name": "app", "path": "packages/app", "target": "npm", "version": "0.5.0",
             "depends_on": ["models"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        topo = snapshot["graph"]["topological_order"]
        assert topo.index("schema") < topo.index("models")
        assert topo.index("models") < topo.index("app")

    def test_max_depth(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "schema", "path": "packages/schema", "target": "pypi", "version": "1.0.0"},
            {"name": "models", "path": "packages/models", "target": "pypi", "version": "2.1.0",
             "depends_on": ["schema"]},
            {"name": "app", "path": "packages/app", "target": "npm", "version": "0.5.0",
             "depends_on": ["models"]},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        # schema=0, models=1, app=2
        assert snapshot["graph"]["max_depth"] == 2

    def test_max_depth_no_deps(self, tmp_path):
        """All independent packages should have max_depth 0."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "a", "path": "packages/a", "target": "pypi", "version": "1.0.0"},
            {"name": "b", "path": "packages/b", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        assert snapshot["graph"]["max_depth"] == 0
        assert snapshot["graph"]["leaf_nodes"] == ["a", "b"]
        assert snapshot["graph"]["root_nodes"] == ["a", "b"]

    def test_empty_workspace(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        assert snapshot["package_count"] == 0
        assert snapshot["packages"] == {}
        assert snapshot["graph"]["leaf_nodes"] == []
        assert snapshot["graph"]["root_nodes"] == []
        assert snapshot["graph"]["max_depth"] == 0
        assert snapshot["graph"]["topological_order"] == []

    def test_defaults_for_optional_fields(self, tmp_path):
        """description defaults to None, library/test_only default to False."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "basic", "path": "packages/basic", "target": "pypi", "version": "0.1.0"},
        ])
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)

        pkg = snapshot["packages"]["basic"]
        assert pkg["description"] is None
        assert pkg["library"] is False
        assert pkg["test_only"] is False


# ---------------------------------------------------------------------------
# write_snapshot
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    def test_creates_file(self, tmp_path):
        snapshot = {
            "generated_at": "2026-05-21T12:00:00Z",
            "package_count": 0,
            "packages": {},
            "graph": {
                "leaf_nodes": [],
                "root_nodes": [],
                "max_depth": 0,
                "topological_order": [],
            },
        }
        rel_path = write_snapshot(str(tmp_path), snapshot)

        full_path = tmp_path / WORKSPACE_DIR / SNAPSHOT_FILE
        assert full_path.exists()
        assert rel_path == os.path.join(WORKSPACE_DIR, SNAPSHOT_FILE)

        content = json.loads(full_path.read_text())
        assert content == snapshot

    def test_overwrites_existing(self, tmp_path):
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / SNAPSHOT_FILE).write_text('{"old": true}')

        snapshot = {"generated_at": "2026-05-21T12:00:00Z", "new": True}
        write_snapshot(str(tmp_path), snapshot)

        content = json.loads((ws_dir / SNAPSHOT_FILE).read_text())
        assert content.get("new") is True
        assert "old" not in content


# ---------------------------------------------------------------------------
# check_snapshot
# ---------------------------------------------------------------------------


class TestCheckSnapshot:
    def test_returns_true_when_fresh(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)

        snapshot = generate_snapshot(root, projects, graph)
        write_snapshot(root, snapshot)

        assert check_snapshot(root, projects, graph) is True

    def test_returns_false_when_stale(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)

        snapshot = generate_snapshot(root, projects, graph)
        write_snapshot(root, snapshot)

        # Bump the version on disk so the snapshot is now stale
        (tmp_path / "packages" / "alpha" / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "2.0.0"\n'
        )

        assert check_snapshot(root, projects, graph) is False

    def test_returns_false_when_missing(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)

        assert check_snapshot(root, projects, graph) is False

    def test_returns_false_on_corrupt_json(self, tmp_path):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)

        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / SNAPSHOT_FILE).write_text("not valid json{{{")

        assert check_snapshot(root, projects, graph) is False

    def test_ignores_generated_at_difference(self, tmp_path):
        """Two snapshots that differ only in generated_at should be considered equal."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        graph = WorkspaceGraph(root, projects)

        snapshot = generate_snapshot(root, projects, graph)
        # Write with a different timestamp
        snapshot["generated_at"] = "1999-01-01T00:00:00Z"
        write_snapshot(root, snapshot)

        # check_snapshot generates a fresh one (current timestamp) and compares
        assert check_snapshot(root, projects, graph) is True


# ---------------------------------------------------------------------------
# Integration: _cmd_snapshot
# ---------------------------------------------------------------------------


class TestCmdSnapshot:
    def test_generate_writes_file(self, tmp_path, monkeypatch, capsys):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
            {"name": "beta", "path": "packages/beta", "target": "npm", "version": "0.2.0",
             "depends_on": ["alpha"]},
        ])
        # Create workspace.toml so find_workspace_root works
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        lines = []
        for p in projects:
            lines.append("[[projects]]")
            lines.append(f'path = "{p["path"]}"')
            lines.append(f'name = "{p["name"]}"')
            if "depends_on" in p:
                deps = ", ".join(f'"{d}"' for d in p["depends_on"])
                lines.append(f"depends_on = [{deps}]")
            lines.append("")
        (ws_dir / "workspace.toml").write_text(workspace_toml("\n".join(lines)))

        monkeypatch.chdir(tmp_path)

        # Mock commit_files_if_changed to avoid needing a real git repo
        import rlsbl.commands.monorepo.snapshot_cmd as snap_mod
        committed = []
        monkeypatch.setattr(snap_mod, "commit_files", lambda *a, **kw: committed.append(a))

        from rlsbl.commands.monorepo import _cmd_snapshot
        _cmd_snapshot({}, project_root=".")

        captured = capsys.readouterr()
        assert "Wrote" in captured.out

        snapshot_path = tmp_path / WORKSPACE_DIR / SNAPSHOT_FILE
        assert snapshot_path.exists()

        data = json.loads(snapshot_path.read_text())
        assert data["package_count"] == 2
        assert "alpha" in data["packages"]
        assert "beta" in data["packages"]

        # Verify commit was attempted
        assert len(committed) == 1

    def test_check_passes_when_fresh(self, tmp_path, monkeypatch, capsys):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[projects]]\npath = "packages/alpha"\nname = "alpha"\n')
        )

        monkeypatch.chdir(tmp_path)

        # Write a fresh snapshot first
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)
        write_snapshot(root, snapshot)

        from rlsbl.commands.monorepo import _cmd_snapshot_check
        _cmd_snapshot_check({}, project_root=".")

        captured = capsys.readouterr()
        assert "up-to-date" in captured.out

    def test_check_fails_when_stale(self, tmp_path, monkeypatch, capsys):
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi", "version": "1.0.0"},
        ])
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[projects]]\npath = "packages/alpha"\nname = "alpha"\n')
        )

        monkeypatch.chdir(tmp_path)

        # Write a snapshot, then change the version
        graph = WorkspaceGraph(root, projects)
        snapshot = generate_snapshot(root, projects, graph)
        write_snapshot(root, snapshot)

        (tmp_path / "packages" / "alpha" / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "9.9.9"\n'
        )

        from rlsbl.commands.monorepo import _cmd_snapshot_check
        with pytest.raises(SystemExit) as exc_info:
            _cmd_snapshot_check({}, project_root=".")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "stale" in captured.err

    def test_dry_run_records_the_write_and_the_commit(
        self, tmp_path, monkeypatch, capsys,
    ):
        """A preview writes nothing, commits nothing, and names both steps.

        The command has no dry-run branch of its own any more: the write and
        the commit go through the effect chokepoint, so `--dry-run` records
        them instead of performing them and the would-do log IS the preview.
        """
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0"},
        ])
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[projects]]\npath = "packages/alpha"\nname = "alpha"\n')
        )
        monkeypatch.chdir(tmp_path)

        from rlsbl import app
        result = app.test(["--dry-run", "monorepo", "snapshot"])

        assert result.exit_code == 0, result.stderr
        assert not (ws_dir / SNAPSHOT_FILE).exists(), \
            "--dry-run must not write snapshot.json"
        assert "DRY RUN" in result.stdout
        assert f"write: {ws_dir / SNAPSHOT_FILE}" in result.stdout, result.stdout
        # WHICH binary performs the commit is an environment fact, not a
        # promise: utils.commit_files uses safegit when it is on PATH and git
        # otherwise, and CI has no safegit. What the preview must record is
        # that a commit would happen at all.
        would_do = result.stdout.split("Would do:")[1]
        assert "commit" in would_do, result.stdout

    def test_dry_run_of_a_current_snapshot_records_no_commit(
        self, tmp_path, monkeypatch, capsys,
    ):
        """An up-to-date artifact still previews its write, but not a commit."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0"},
        ])
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[projects]]\npath = "packages/alpha"\nname = "alpha"\n')
        )
        monkeypatch.chdir(tmp_path)

        graph = WorkspaceGraph(root, projects)
        write_snapshot(root, generate_snapshot(root, projects, graph))
        before = (ws_dir / SNAPSHOT_FILE).read_bytes()

        from rlsbl import app
        result = app.test(["--dry-run", "monorepo", "snapshot"])

        assert result.exit_code == 0, result.stderr
        assert (ws_dir / SNAPSHOT_FILE).read_bytes() == before
        assert "Snapshot unchanged." in result.stdout
        assert "commit" not in result.stdout.split("Would do:")[1]

    def test_snapshot_check_is_read_only_under_dry_run(
        self, tmp_path, monkeypatch,
    ):
        """The check half records nothing at all -- it changes nothing."""
        root, projects = _make_workspace(tmp_path, [
            {"name": "alpha", "path": "packages/alpha", "target": "pypi",
             "version": "1.0.0"},
        ])
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            workspace_toml('[[projects]]\npath = "packages/alpha"\nname = "alpha"\n')
        )
        monkeypatch.chdir(tmp_path)
        graph = WorkspaceGraph(root, projects)
        write_snapshot(root, generate_snapshot(root, projects, graph))

        from rlsbl import app
        result = app.test(["--dry-run", "monorepo", "snapshot-check"])

        assert result.exit_code == 0, result.stderr
        assert result.stdout.rstrip().endswith(
            "DRY RUN — no changes were made. Would do:"
        ), result.stdout
