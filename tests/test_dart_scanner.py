"""Tests for the DartScanner workspace scanner."""

import textwrap

from rlsbl.workspace_graph import DartScanner, Dependency, WorkspaceScanner


class TestDartScannerProtocol:
    def test_conforms_to_workspace_scanner(self):
        assert isinstance(DartScanner(), WorkspaceScanner)


class TestDartScannerVersioned:
    def test_versioned_string_dep(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              models: ^1.0.0
        """))
        deps = DartScanner().scan(str(proj), {"app", "models"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="models", dep_type="versioned", constraint="^1.0.0")

    def test_any_constraint(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              models: any
        """))
        deps = DartScanner().scan(str(proj), {"models"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="models", dep_type="versioned", constraint="any")

    def test_null_constraint(self, tmp_path):
        """A dep with no constraint (YAML null) gets empty string."""
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              models:
        """))
        deps = DartScanner().scan(str(proj), {"models"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="models", dep_type="versioned", constraint="")


class TestDartScannerPath:
    def test_path_dep(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              utils:
                path: ../utils
        """))
        deps = DartScanner().scan(str(proj), {"utils"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="utils", dep_type="path", constraint="../utils")


class TestDartScannerHosted:
    def test_hosted_with_version(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              schema:
                hosted: https://pub.dev
                version: ^2.0.0
        """))
        deps = DartScanner().scan(str(proj), {"schema"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="schema", dep_type="versioned", constraint="^2.0.0")

    def test_hosted_without_version(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              schema:
                hosted: https://pub.dev
        """))
        deps = DartScanner().scan(str(proj), {"schema"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="schema", dep_type="versioned", constraint="")


class TestDartScannerNonWorkspace:
    def test_non_workspace_dep_excluded(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              http: ^0.13.0
              models: ^1.0.0
        """))
        deps = DartScanner().scan(str(proj), {"models"})
        assert len(deps) == 1
        assert deps[0].name == "models"


class TestDartScannerMissing:
    def test_no_pubspec(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        deps = DartScanner().scan(str(proj), {"models"})
        assert deps == []


class TestDartScannerDevDependencies:
    def test_dev_dep_found(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dev_dependencies:
              test_utils: ^1.0.0
        """))
        deps = DartScanner().scan(str(proj), {"test_utils"})
        assert len(deps) == 1
        assert deps[0] == Dependency(name="test_utils", dep_type="versioned", constraint="^1.0.0")


class TestDartScannerMultiple:
    def test_multiple_workspace_deps(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "pubspec.yaml").write_text(textwrap.dedent("""\
            name: app
            dependencies:
              models: ^1.0.0
              utils:
                path: ../utils
            dev_dependencies:
              test_helpers: any
        """))
        deps = DartScanner().scan(str(proj), {"models", "utils", "test_helpers", "unrelated"})
        names = {d.name for d in deps}
        assert names == {"models", "utils", "test_helpers"}
        by_name = {d.name: d for d in deps}
        assert by_name["models"].dep_type == "versioned"
        assert by_name["utils"].dep_type == "path"
        assert by_name["test_helpers"].dep_type == "versioned"
