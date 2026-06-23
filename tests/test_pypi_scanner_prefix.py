"""Tests for PyPI scanner matching actual PyPI names that differ from workspace names."""

import os
import textwrap

from rlsbl.workspace_graph import Dependency, PypiScanner, WorkspaceGraph


class TestPypiScannerPrefixMapping:
    """PypiScanner should detect intra-workspace deps declared via actual PyPI names."""

    def test_pypi_name_detected_via_map(self, tmp_path):
        """Dependency declared as PyPI name (orxtra-transport) is detected
        when workspace name is 'transport'."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        # Project depends on "orxtra-transport" which is the PyPI name
        # for workspace project "transport"
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            dependencies = ["orxtra-transport>=1.0"]
        """))

        workspace_names = {"transport", "consumer", "other"}
        pypi_name_map = {"orxtra-transport": "transport"}

        deps = scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map)
        assert len(deps) == 1
        assert deps[0].name == "transport"
        assert deps[0].dep_type == "versioned"
        assert deps[0].scope == "runtime"

    def test_workspace_name_still_works(self, tmp_path):
        """Dependency declared directly as workspace name still matches."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            dependencies = ["transport>=1.0"]
        """))

        workspace_names = {"transport", "consumer"}
        pypi_name_map = {"orxtra-transport": "transport"}

        deps = scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map)
        assert len(deps) == 1
        assert deps[0].name == "transport"

    def test_no_pyproject_handled_gracefully(self, tmp_path):
        """Project without pyproject.toml returns no deps."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        workspace_names = {"transport", "consumer"}
        pypi_name_map = {"orxtra-transport": "transport"}

        deps = scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map)
        assert deps == []

    def test_normalization_case_insensitive(self, tmp_path):
        """PyPI name normalization handles case and underscores (PEP 503)."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            dependencies = ["Orxtra_Transport>=1.0"]
        """))

        workspace_names = {"transport", "consumer"}
        # Map uses normalized form
        pypi_name_map = {"orxtra-transport": "transport"}

        deps = scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map)
        assert len(deps) == 1
        assert deps[0].name == "transport"

    def test_pypi_name_map_none(self, tmp_path):
        """Scanner works when pypi_name_map is not provided (backward compat)."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            dependencies = ["transport>=1.0"]
        """))

        workspace_names = {"transport", "consumer"}
        # No pypi_name_map passed -- original behavior
        deps = scanner.scan(project_dir, workspace_names)
        assert len(deps) == 1
        assert deps[0].name == "transport"

    def test_optional_deps_with_pypi_name(self, tmp_path):
        """PyPI name mapping also works for optional/dev dependencies."""
        scanner = PypiScanner()
        project_dir = str(tmp_path)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            dependencies = []
            [project.optional-dependencies]
            dev = ["orxtra-transport>=1.0"]
        """))

        workspace_names = {"transport", "consumer"}
        pypi_name_map = {"orxtra-transport": "transport"}

        deps = scanner.scan(project_dir, workspace_names, pypi_name_map=pypi_name_map)
        assert len(deps) == 1
        assert deps[0].name == "transport"
        assert deps[0].scope == "dev"


class TestWorkspaceGraphPypiNameMap:
    """WorkspaceGraph builds pypi_name_map from pyproject.toml files."""

    def test_graph_detects_pypi_name_dep(self, tmp_path):
        """Full integration: graph detects dep declared via PyPI name."""
        root = str(tmp_path)

        # Create transport project with a different PyPI name
        transport_dir = tmp_path / "transport"
        transport_dir.mkdir()
        (transport_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "orxtra-transport"
            version = "1.0.0"
            dependencies = []
        """))

        # Create consumer project that depends on orxtra-transport
        consumer_dir = tmp_path / "consumer"
        consumer_dir.mkdir()
        (consumer_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            version = "1.0.0"
            dependencies = ["orxtra-transport>=1.0"]
        """))

        projects = [
            {"name": "transport", "path": "transport"},
            {"name": "consumer", "path": "consumer"},
        ]

        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("consumer")
        assert len(deps) == 1
        assert deps[0].name == "transport"

    def test_graph_no_pyproject_graceful(self, tmp_path):
        """Graph handles projects without pyproject.toml."""
        root = str(tmp_path)

        # Transport has no pyproject.toml at all
        transport_dir = tmp_path / "transport"
        transport_dir.mkdir()

        # Consumer has a pyproject.toml but transport won't be in pypi_name_map
        consumer_dir = tmp_path / "consumer"
        consumer_dir.mkdir()
        (consumer_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            version = "1.0.0"
            dependencies = ["orxtra-transport>=1.0"]
        """))

        projects = [
            {"name": "transport", "path": "transport"},
            {"name": "consumer", "path": "consumer"},
        ]

        # Should not crash; orxtra-transport won't be detected as intra-workspace
        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("consumer")
        assert len(deps) == 0

    def test_graph_same_pypi_and_workspace_name(self, tmp_path):
        """When PyPI name equals workspace name, no extra map entry needed."""
        root = str(tmp_path)

        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        (lib_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "mylib"
            version = "1.0.0"
            dependencies = []
        """))

        consumer_dir = tmp_path / "consumer"
        consumer_dir.mkdir()
        (consumer_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "consumer"
            version = "1.0.0"
            dependencies = ["mylib>=1.0"]
        """))

        projects = [
            {"name": "mylib", "path": "mylib"},
            {"name": "consumer", "path": "consumer"},
        ]

        graph = WorkspaceGraph(root, projects)
        deps = graph.dependencies("consumer")
        assert len(deps) == 1
        assert deps[0].name == "mylib"
