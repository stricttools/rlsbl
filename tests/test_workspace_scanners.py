"""Tests for the pluggable WorkspaceScanner interface."""

import json
import os
import textwrap

from rlsbl.workspace_graph import (
    Dependency,
    NpmScanner,
    PypiScanner,
    SCANNERS,
    WorkspaceScanner,
)


class TestScannersRegistry:
    """Verify the SCANNERS module-level list."""

    def test_scanners_contains_pypi_and_npm(self):
        types = [type(s) for s in SCANNERS]
        assert PypiScanner in types
        assert NpmScanner in types

    def test_scanners_length(self):
        assert len(SCANNERS) >= 2


class TestProtocolConformance:
    """Verify scanners implement the WorkspaceScanner protocol."""

    def test_pypi_scanner_is_workspace_scanner(self):
        assert isinstance(PypiScanner(), WorkspaceScanner)

    def test_npm_scanner_is_workspace_scanner(self):
        assert isinstance(NpmScanner(), WorkspaceScanner)

    def test_custom_scanner_conforms(self):
        """A custom class with a scan() method satisfies the protocol."""

        class CustomScanner:
            def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
                return []

        assert isinstance(CustomScanner(), WorkspaceScanner)


class TestCustomScannerIntegration:
    """A custom scanner appended to SCANNERS gets called during graph construction."""

    def test_custom_scanner_called(self, tmp_path, monkeypatch):
        import rlsbl.workspace_graph as wg_mod
        from rlsbl.workspace_graph import WorkspaceGraph

        calls = []

        class SpyScanner:
            def scan(self, project_dir, workspace_names):
                calls.append((project_dir, workspace_names))
                return []

        spy = SpyScanner()
        monkeypatch.setattr(wg_mod, "SCANNERS", [spy])

        projects = [
            {"path": "packages/a", "name": "a"},
            {"path": "packages/b", "name": "b"},
        ]
        for proj in projects:
            (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)

        WorkspaceGraph(str(tmp_path), projects)
        # The spy should have been called once per project
        assert len(calls) == 2
        dirs = {c[0] for c in calls}
        assert str(tmp_path / "packages" / "a") in dirs
        assert str(tmp_path / "packages" / "b") in dirs
        # workspace_names should contain both project names
        for _, names in calls:
            assert names == {"a", "b"}


class TestPypiScannerDirectly:
    """Test PypiScanner.scan() directly."""

    def test_scan_returns_deps(self, tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        pyproject = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core>=1.0"]
        """)
        (proj_dir / "pyproject.toml").write_text(pyproject)

        scanner = PypiScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"})
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "versioned"

    def test_scan_no_manifest(self, tmp_path):
        proj_dir = tmp_path / "empty"
        proj_dir.mkdir()
        scanner = PypiScanner()
        assert scanner.scan(str(proj_dir), {"a"}) == []


class TestNpmScannerDirectly:
    """Test NpmScanner.scan() directly."""

    def test_scan_returns_deps(self, tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        pkg = json.dumps({"name": "app", "dependencies": {"lib": "workspace:*"}})
        (proj_dir / "package.json").write_text(pkg)

        scanner = NpmScanner()
        deps = scanner.scan(str(proj_dir), {"app", "lib"})
        assert len(deps) == 1
        assert deps[0].name == "lib"
        assert deps[0].dep_type == "workspace"

    def test_scan_no_manifest(self, tmp_path):
        proj_dir = tmp_path / "empty"
        proj_dir.mkdir()
        scanner = NpmScanner()
        assert scanner.scan(str(proj_dir), {"a"}) == []
