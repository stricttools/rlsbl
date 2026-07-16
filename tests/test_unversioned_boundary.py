"""Tests for the unversioned-boundary workspace check.

A workspace project with ``releasable = false`` that is NOT dev-only is
skipped by changelog coverage entirely, so when releasable projects have
a runtime dependency on it, its changes ship inside consumer releases
with zero changelog trail. The unversioned-boundary check errors on that,
mirroring dev-only-boundary (which covers the dev-only variant of the
same hole).
"""

import pytest

from conftest import capture_all_checks, make_workspace, run_git
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.workspace import WORKSPACE_DIR, load_workspace
from rlsbl.workspace_graph import WorkspaceGraph


def _register_and_get_checks():
    """Register all checks on a mock app and return the check function dict."""
    return capture_all_checks()


def _make_pypi_project(root, subdir, version="0.1.0", deps=None, dev_deps=None):
    """Create a minimal pyproject.toml with optional runtime and dev deps."""
    proj_dir = root / subdir
    proj_dir.mkdir(parents=True, exist_ok=True)
    dep_lines = ""
    if deps:
        items = ", ".join(f'"{d}"' for d in deps)
        dep_lines = f"dependencies = [{items}]"
    dev_lines = ""
    if dev_deps:
        items = ", ".join(f'"{d}"' for d in dev_deps)
        dev_lines = f'[project.optional-dependencies]\ndev = [{items}]'
    content = (
        f'[project]\nname = "{subdir}"\nversion = "{version}"\n'
        f'{dep_lines}\n\n{dev_lines}\n'
    )
    (proj_dir / "pyproject.toml").write_text(content)


def _setup_monorepo(tmp_path, monkeypatch, projects_spec):
    """Set up a monorepo with given projects and return (root, ctx).

    projects_spec is a list of dicts with keys:
      path, name, releasable (optional: False or str),
      dev_only (optional bool), dev_node (optional bool),
      deps (optional list of runtime dep names),
      dev_deps (optional list of dev dep names).
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# monorepo\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    ws_projects = []
    for spec in projects_spec:
        ws_proj = {"path": spec["path"], "name": spec["name"]}
        for key in ("dev_node", "dev_only", "releasable"):
            if key in spec:
                ws_proj[key] = spec[key]
        ws_projects.append(ws_proj)

    make_workspace(tmp_path, ws_projects)

    for spec in projects_spec:
        _make_pypi_project(
            tmp_path,
            spec["path"],
            deps=spec.get("deps"),
            dev_deps=spec.get("dev_deps"),
        )

    run_git(tmp_path, "add", WORKSPACE_DIR)
    for spec in projects_spec:
        run_git(tmp_path, "add", spec["path"])
    run_git(tmp_path, "commit", "-q", "-m", "add projects")

    projects = load_workspace(str(tmp_path))
    graph = WorkspaceGraph(str(tmp_path), projects)

    ctx = WorkspaceCheckContext(
        project_root=tmp_path,
        workspace_root=tmp_path,
        config={},
        projects=projects,
        graph=graph,
    )
    return tmp_path, ctx


class TestUnversionedBoundary:
    """Tests for check_unversioned_boundary."""

    def test_check_is_registered(self):
        """The check must exist under the unversioned-boundary name."""
        checks = _register_and_get_checks()
        assert "unversioned-boundary" in checks

    def test_boundary_catches_runtime_dependency(self, tmp_path, monkeypatch):
        """Releasable A runtime-depends on unversioned (releasable=false) B -> FAIL."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "fail"
        assert "proj-a" in result.problems[0].text
        assert "proj-b" in result.problems[0].text

    def test_boundary_allows_dev_dependency(self, tmp_path, monkeypatch):
        """Releasable A depends on unversioned B via dev dep only -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "dev_deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_allows_no_dependents(self, tmp_path, monkeypatch):
        """Unversioned B with no dependents at all -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a"},
            {"path": "proj-b", "name": "proj-b", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "pass"

    def test_dev_only_unversioned_is_left_to_dev_only_boundary(self, tmp_path, monkeypatch):
        """releasable=false + dev_only is dev-only-boundary's territory -> PASS here."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "releasable": False, "dev_only": True},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)
        assert result.status == "pass"

        # ...and dev-only-boundary does flag it
        dev_result = checks["dev-only-boundary"](ctx)
        assert dev_result.status == "fail"

    def test_dev_only_dependent_not_flagged(self, tmp_path, monkeypatch):
        """A dev-only consumer of an unversioned project is fine (nothing ships)."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"],
             "dev_only": True, "releasable": False},
            {"path": "proj-b", "name": "proj-b", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "pass"

    def test_boundary_transitive_chain(self, tmp_path, monkeypatch):
        """A->B->C where C is unversioned, all runtime deps -> FAIL mentions A."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "deps": ["proj-c"]},
            {"path": "proj-c", "name": "proj-c", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "fail"
        violation_text = "\n".join(p.text for p in result.problems)
        assert "proj-a" in violation_text
        assert "proj-c" in violation_text

    def test_boundary_releasable_through_unversioned_chain(self, tmp_path, monkeypatch):
        """A (releasable) -> B (unversioned) -> C (unversioned), all runtime deps.

        Transitive rdeps of C include both B and A. Only A is releasable, so
        A must be flagged (for both B and C) while B -- itself unversioned --
        must not appear as a violating dependent.
        """
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b", "releasable": False,
             "deps": ["proj-c"]},
            {"path": "proj-c", "name": "proj-c", "releasable": False},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "fail"
        # A is flagged for its direct dep on B and its transitive dep on C
        assert any("'proj-a'" in v and "'proj-b'" in v for v in (p.text for p in result.problems))
        assert any("'proj-a'" in v and "'proj-c'" in v for v in (p.text for p in result.problems))
        # B is unversioned itself -- never flagged as a violating dependent
        assert not any(
            v.startswith("releasable project 'proj-b'") for v in " ".join(p.text for p in result.problems)
        )

    def test_no_unversioned_projects(self, tmp_path, monkeypatch):
        """No releasable=false projects at all -> PASS."""
        _root, ctx = _setup_monorepo(tmp_path, monkeypatch, [
            {"path": "proj-a", "name": "proj-a", "deps": ["proj-b"]},
            {"path": "proj-b", "name": "proj-b"},
        ])
        checks = _register_and_get_checks()
        result = checks["unversioned-boundary"](ctx)

        assert result.status == "pass"


class TestUnversionedBoundaryRegistration:
    """The check must be registered in the double-entry check system."""

    def test_in_checks_toml(self):
        import tomllib
        from pathlib import Path

        import rlsbl

        checks_toml = Path(rlsbl.__file__).parent / "data" / "checks.toml"
        with open(checks_toml, "rb") as f:
            data = tomllib.load(f)
        entry = data["checks"]["unversioned-boundary"]
        assert entry["tags"] == ["workspace"]
        assert entry["severity"] == "error"
        assert entry["scope"] == "workspace"

    def test_in_check_targets(self):
        from rlsbl.checks import CHECK_TARGETS

        assert CHECK_TARGETS.get("unversioned-boundary") == "workspace"
