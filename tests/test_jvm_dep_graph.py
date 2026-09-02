"""Tests for JVM (Gradle/Maven) dependency graph parsing in workspace_graph.MavenScanner."""

import os
import textwrap

import pytest

from rlsbl.workspace_graph import (
    Dependency,
    ManifestScanError,
    MavenScanner,
    SCANNERS,
    UnrecognizedGradleDependencyError,
    WorkspaceGraph,
    WorkspaceScanner,
)


def _write_file(tmp_path, rel_path, content):
    """Write content to a file under tmp_path, creating parent dirs."""
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(content))
    return full


class TestMavenScannerProtocol:
    """MavenScanner conforms to the WorkspaceScanner protocol."""

    def test_is_workspace_scanner(self):
        assert isinstance(MavenScanner(), WorkspaceScanner)

    def test_in_scanners_list(self):
        types = [type(s) for s in SCANNERS]
        assert MavenScanner in types


class TestGradleKtsProjectDeps:
    """Gradle Kotlin DSL: implementation(project(":module")) patterns."""

    def test_implementation_project(self, tmp_path):
        """implementation(project(":module")) parsed correctly."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            plugins {
                kotlin("jvm")
            }
            dependencies {
                implementation(project(":core"))
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core", "utils"})
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "project"
        assert deps[0].constraint == ":core"
        assert deps[0].scope == "runtime"

    def test_api_project(self, tmp_path):
        """api(project(":module")) parsed correctly."""
        proj_dir = tmp_path / "lib"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                api(project(":shared"))
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"lib", "shared"})
        assert len(deps) == 1
        assert deps[0].name == "shared"
        assert deps[0].dep_type == "project"
        assert deps[0].scope == "runtime"

    def test_test_implementation_project(self, tmp_path):
        """testImplementation(project(":test-utils")) uses dev scope."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                testImplementation(project(":test-utils"))
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "test-utils"})
        assert len(deps) == 1
        assert deps[0].name == "test-utils"
        assert deps[0].scope == "dev"

    def test_subproject_path(self, tmp_path):
        """project(":sub:module") extracts last segment as name."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":libs:networking"))
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "networking"})
        assert len(deps) == 1
        assert deps[0].name == "networking"
        assert deps[0].constraint == ":libs:networking"


class TestGradleKtsExternalDeps:
    """Gradle Kotlin DSL: implementation("group:artifact:version") patterns."""

    def test_external_dep_parsed(self, tmp_path):
        """External dependency string "group:artifact:version" parsed."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation("com.example:mylib:1.2.3")
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "mylib"})
        assert len(deps) == 1
        assert deps[0].name == "mylib"
        assert deps[0].dep_type == "versioned"
        assert deps[0].constraint == "1.2.3"
        assert deps[0].scope == "runtime"

    def test_external_dep_not_in_workspace(self, tmp_path):
        """External dep whose artifact is not a workspace name is ignored."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation("com.google.guava:guava:31.1-jre")
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "mylib"})
        assert len(deps) == 0


class TestGradleGroovyDeps:
    """Gradle Groovy DSL: implementation project(':module') and string patterns."""

    def test_implementation_project(self, tmp_path):
        """implementation project(':module') parsed correctly."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"})
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "project"
        assert deps[0].constraint == ":core"

    def test_api_project_double_quotes(self, tmp_path):
        """api project(\":module\") with double quotes."""
        proj_dir = tmp_path / "lib"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                api project(":shared")
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"lib", "shared"})
        assert len(deps) == 1
        assert deps[0].name == "shared"

    def test_external_dep_single_quote(self, tmp_path):
        """implementation 'group:artifact:version' with single quotes."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation 'com.example:mylib:2.0'
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "mylib"})
        assert len(deps) == 1
        assert deps[0].name == "mylib"
        assert deps[0].dep_type == "versioned"
        assert deps[0].constraint == "2.0"

    def test_external_dep_parenthesized(self, tmp_path):
        """implementation('group:artifact:version') parenthesized form."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation('com.example:mylib:3.0')
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "mylib"})
        assert len(deps) == 1
        assert deps[0].name == "mylib"
        assert deps[0].constraint == "3.0"


class TestPomXmlDeps:
    """Maven pom.xml: <dependency> elements parsed correctly."""

    def test_simple_dependency(self, tmp_path):
        """Basic <dependency> with groupId, artifactId, version."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>core</artifactId>
                        <version>1.0.0</version>
                    </dependency>
                </dependencies>
            </project>
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"})
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "versioned"
        assert deps[0].constraint == "1.0.0"
        assert deps[0].scope == "runtime"

    def test_test_scope_is_dev(self, tmp_path):
        """<scope>test</scope> maps to scope="dev"."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>testlib</artifactId>
                        <version>2.0.0</version>
                        <scope>test</scope>
                    </dependency>
                </dependencies>
            </project>
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "testlib"})
        assert len(deps) == 1
        assert deps[0].scope == "dev"

    def test_provided_scope_is_dev(self, tmp_path):
        """<scope>provided</scope> maps to scope="dev"."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>api-lib</artifactId>
                        <version>1.0.0</version>
                        <scope>provided</scope>
                    </dependency>
                </dependencies>
            </project>
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "api-lib"})
        assert len(deps) == 1
        assert deps[0].scope == "dev"

    def test_no_namespace_pom(self, tmp_path):
        """pom.xml without namespace still parses."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project>
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>core</artifactId>
                        <version>1.0.0</version>
                    </dependency>
                </dependencies>
            </project>
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"})
        assert len(deps) == 1
        assert deps[0].name == "core"

    def test_multiple_dependencies(self, tmp_path):
        """Multiple <dependency> elements all parsed."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>core</artifactId>
                        <version>1.0.0</version>
                    </dependency>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>utils</artifactId>
                        <version>2.0.0</version>
                    </dependency>
                    <dependency>
                        <groupId>org.external</groupId>
                        <artifactId>not-in-workspace</artifactId>
                        <version>3.0.0</version>
                    </dependency>
                </dependencies>
            </project>
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core", "utils"})
        assert len(deps) == 2
        names = {d.name for d in deps}
        assert names == {"core", "utils"}

    def test_malformed_pom(self, tmp_path, capsys):
        """A malformed pom.xml is raised to the caller, not swallowed.

        The scanner used to warn and return an empty list, which is
        indistinguishable from "this project declares no dependencies" -- and
        the CI router's paths filters are derived from those dependencies, so
        an unreadable manifest silently narrowed them. The graph is still
        tolerant (it warns and carries on); it also records the failure so a
        consumer that must not narrow can refuse.
        """
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text("this is not valid xml <<<")

        scanner = MavenScanner()
        with pytest.raises(ManifestScanError) as exc:
            scanner.scan(str(proj_dir), {"app", "core"})
        assert exc.value.path.endswith("pom.xml")

        graph = WorkspaceGraph(str(tmp_path), [{"path": "app", "name": "app"}])
        assert graph.dependencies("app") == []
        assert [e.project for e in graph.scan_errors] == ["app"]
        captured = capsys.readouterr()
        assert "Warning" in captured.err


class TestUnrecognizedGradlePatterns:
    """Unrecognized Gradle dependency patterns are raised, not swallowed.

    A Gradle file that parses but declares a dependency in a form the scanner
    cannot read is the unreadable manifest wearing different clothes: the edge
    exists in the build and not in the graph, so anything derived from the
    edges is narrower than the workspace. It travels the same road -- the
    scanner raises, the graph stays tolerant (warns, keeps the edges it DID
    recognize, records the failure), and a narrowing consumer refuses.
    """

    def test_kts_variable_reference_warns(self, tmp_path, capsys):
        """libs.<alias> in KTS resolves via version catalog when available."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            someLib = "com.example:core:1.0.0"
        """))

        proj_dir = workspace_root / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":core"))
                implementation(libs.someLib)
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"}, workspace_root=str(workspace_root))
        assert len(deps) == 2
        dep_by_type = {d.dep_type: d for d in deps}
        assert "project" in dep_by_type
        assert "catalog" in dep_by_type
        assert dep_by_type["catalog"].name == "core"
        assert dep_by_type["catalog"].constraint == "com.example:core"
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()

    def test_kts_variable_reference_raises_without_catalog(self, tmp_path):
        """Variable reference in KTS without a catalog is raised to the caller."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":core"))
                implementation(libs.someLib)
            }
        """))

        scanner = MavenScanner()
        with pytest.raises(UnrecognizedGradleDependencyError) as exc:
            scanner.scan(str(proj_dir), {"app", "core"})
        assert exc.value.path.endswith("build.gradle.kts")
        assert exc.value.lines == [(3, "implementation(libs.someLib)")]
        # The recognized declarations are real edges and travel with the
        # failure, so the tolerant graph keeps them.
        assert [d.name for d in exc.value.deps] == ["core"]
        assert "unrecognized" in str(exc.value).lower()
        assert "libs.someLib" in str(exc.value)

    def test_groovy_variable_reference_raises(self, tmp_path):
        """Variable reference in a Groovy dependency is raised to the caller."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
                implementation deps.someLib
            }
        """))

        scanner = MavenScanner()
        with pytest.raises(UnrecognizedGradleDependencyError) as exc:
            scanner.scan(str(proj_dir), {"app", "core"})
        assert exc.value.path.endswith("build.gradle")
        assert exc.value.lines == [(3, "implementation deps.someLib")]
        assert [d.name for d in exc.value.deps] == ["core"]
        assert "deps.someLib" in str(exc.value)

    def test_it_is_a_manifest_scan_error(self, tmp_path):
        """The new class rides the road the read failures already built."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation deps.someLib
            }
        """))

        with pytest.raises(ManifestScanError):
            MavenScanner().scan(str(proj_dir), {"app", "core"})

    def test_the_graph_warns_keeps_the_recognized_edges_and_records_it(self, tmp_path, capsys):
        """Tolerance, unchanged: the rendering consumers still get an answer."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
                implementation deps.someLib
            }
        """))

        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app"},
            {"path": "core", "name": "core"},
        ])
        assert [d.name for d in graph.dependencies("app")] == ["core"]
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "unrecognized" in captured.err.lower()
        assert "deps.someLib" in captured.err

        assert [e.project for e in graph.scan_errors] == ["app"]
        error = graph.scan_errors[0]
        assert error.path.endswith("build.gradle")
        assert "line 3" in error.message
        assert "depends_on" in error.remedy

    def test_a_recognized_gradle_file_records_nothing(self, tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
            }
        """))

        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app"},
            {"path": "core", "name": "core"},
        ])
        assert graph.scan_errors == []

    def test_groovy_catalog_reference_resolves(self, tmp_path, capsys):
        """libs.<alias> in Groovy resolves via version catalog."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            someLib = { module = "com.example:shared", version = "2.0" }
        """))

        proj_dir = workspace_root / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
                implementation libs.someLib
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core", "shared"}, workspace_root=str(workspace_root))
        assert len(deps) == 2
        catalog_deps = [d for d in deps if d.dep_type == "catalog"]
        assert len(catalog_deps) == 1
        assert catalog_deps[0].name == "shared"
        assert catalog_deps[0].constraint == "com.example:shared"
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()


class TestVersionCatalogParsing:
    """Version catalog parsing and alias resolution edge cases."""

    def test_explicit_group_name_form(self, tmp_path, capsys):
        """Catalog entry with explicit group/name fields resolves."""
        workspace_root = tmp_path / "ws"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            myLib = { group = "com.example", name = "core", version = "1.0" }
        """))

        proj_dir = workspace_root / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(libs.myLib)
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"}, workspace_root=str(workspace_root))
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "catalog"
        assert deps[0].constraint == "com.example:core"
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()

    def test_alias_normalization_dashes(self, tmp_path, capsys):
        """Catalog alias with dashes matches dotted reference."""
        workspace_root = tmp_path / "ws"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            some-lib = "com.example:core:1.0"
        """))

        proj_dir = workspace_root / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(libs.some.lib)
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"}, workspace_root=str(workspace_root))
        assert len(deps) == 1
        assert deps[0].name == "core"
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()

    def test_catalog_external_no_warn(self, tmp_path, capsys):
        """Catalog reference to non-workspace dep does not warn."""
        workspace_root = tmp_path / "ws"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            guava = "com.google.guava:guava:31.1-jre"
        """))

        proj_dir = workspace_root / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(libs.guava)
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core"}, workspace_root=str(workspace_root))
        assert len(deps) == 0  # guava is not a workspace project
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()

    def test_per_project_catalog(self, tmp_path, capsys):
        """Per-project catalog (no workspace root) resolves."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        catalog_dir = proj_dir / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            myLib = "com.example:core:1.0"
        """))
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(libs.myLib)
            }
        """))

        scanner = MavenScanner()
        # No workspace_root passed -- falls back to per-project catalog
        deps = scanner.scan(str(proj_dir), {"app", "core"})
        assert len(deps) == 1
        assert deps[0].name == "core"
        assert deps[0].dep_type == "catalog"
        captured = capsys.readouterr()
        assert "unrecognized" not in captured.err.lower()

    def test_catalog_caching(self, tmp_path):
        """Catalog is parsed once per workspace root, not per project."""
        workspace_root = tmp_path / "ws"
        workspace_root.mkdir()
        catalog_dir = workspace_root / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [libraries]
            myLib = "com.example:core:1.0"
        """))

        for name in ("app1", "app2"):
            proj_dir = workspace_root / name
            proj_dir.mkdir()
            (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
                dependencies {
                    implementation(libs.myLib)
                }
            """))

        scanner = MavenScanner()
        deps1 = scanner.scan(str(workspace_root / "app1"), {"app1", "app2", "core"}, workspace_root=str(workspace_root))
        deps2 = scanner.scan(str(workspace_root / "app2"), {"app1", "app2", "core"}, workspace_root=str(workspace_root))
        assert len(deps1) == 1
        assert len(deps2) == 1
        # Only one cache entry
        assert len(scanner._catalog_cache) == 1


class TestMixedProjectAndExternalDeps:
    """Mixed project and external dependencies both extracted."""

    def test_kts_mixed_deps(self, tmp_path):
        """KTS file with both project and external deps."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":core"))
                implementation("com.example:shared:1.0.0")
                testImplementation("com.example:test-utils:2.0.0")
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core", "shared", "test-utils"})
        assert len(deps) == 3
        dep_by_name = {d.name: d for d in deps}
        assert dep_by_name["core"].dep_type == "project"
        assert dep_by_name["core"].scope == "runtime"
        assert dep_by_name["shared"].dep_type == "versioned"
        assert dep_by_name["shared"].scope == "runtime"
        assert dep_by_name["test-utils"].dep_type == "versioned"
        assert dep_by_name["test-utils"].scope == "dev"

    def test_groovy_mixed_deps(self, tmp_path):
        """Groovy file with both project and external deps."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
                implementation 'com.example:shared:1.0.0'
                testImplementation 'com.example:test-utils:2.0.0'
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "core", "shared", "test-utils"})
        assert len(deps) == 3
        dep_by_name = {d.name: d for d in deps}
        assert dep_by_name["core"].dep_type == "project"
        assert dep_by_name["shared"].dep_type == "versioned"
        assert dep_by_name["test-utils"].scope == "dev"


class TestMultiModuleGradleGraph:
    """Multi-module Gradle project produces correct workspace graph."""

    def test_multi_module_topo_order(self, tmp_path):
        """A multi-module Gradle project: app -> lib -> core."""
        projects = [
            {"path": "core", "name": "core"},
            {"path": "lib", "name": "lib"},
            {"path": "app", "name": "app"},
        ]

        for proj in projects:
            proj_dir = tmp_path / proj["path"]
            proj_dir.mkdir(parents=True, exist_ok=True)

        # core has no deps
        (tmp_path / "core" / "build.gradle.kts").write_text(textwrap.dedent("""\
            plugins { kotlin("jvm") }
        """))

        # lib depends on core
        (tmp_path / "lib" / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                api(project(":core"))
            }
        """))

        # app depends on lib and core
        (tmp_path / "app" / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":lib"))
                implementation(project(":core"))
            }
        """))

        graph = WorkspaceGraph(str(tmp_path), projects)

        # Check dependencies
        assert graph.dep_count("core") == 0
        assert graph.dep_count("lib") == 1
        assert graph.dep_count("app") == 2

        # Check reverse deps
        assert graph.rdep_count("core") == 2
        assert graph.rdep_count("lib") == 1
        assert graph.rdep_count("app") == 0

        # Topological order: core first, then lib, then app
        order = graph.topological_order()
        assert order.index("core") < order.index("lib")
        assert order.index("lib") < order.index("app")

    def test_multi_module_with_test_deps(self, tmp_path):
        """Multi-module with both runtime and test deps."""
        projects = [
            {"path": "core", "name": "core"},
            {"path": "test-utils", "name": "test-utils"},
            {"path": "app", "name": "app"},
        ]

        for proj in projects:
            proj_dir = tmp_path / proj["path"]
            proj_dir.mkdir(parents=True, exist_ok=True)

        (tmp_path / "core" / "build.gradle.kts").write_text("")
        (tmp_path / "test-utils" / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":core"))
            }
        """))
        (tmp_path / "app" / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":core"))
                testImplementation(project(":test-utils"))
            }
        """))

        graph = WorkspaceGraph(str(tmp_path), projects)
        app_deps = graph.dependencies("app")
        assert len(app_deps) == 2
        dep_by_name = {d.name: d for d in app_deps}
        assert dep_by_name["core"].scope == "runtime"
        assert dep_by_name["test-utils"].scope == "dev"

    def test_groovy_multi_module(self, tmp_path):
        """Multi-module Groovy Gradle project."""
        projects = [
            {"path": "core", "name": "core"},
            {"path": "app", "name": "app"},
        ]

        for proj in projects:
            (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)

        (tmp_path / "core" / "build.gradle").write_text("")
        (tmp_path / "app" / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
            }
        """))

        graph = WorkspaceGraph(str(tmp_path), projects)
        order = graph.topological_order()
        assert order.index("core") < order.index("app")

    def test_maven_multi_module(self, tmp_path):
        """Multi-module Maven project."""
        projects = [
            {"path": "core", "name": "core"},
            {"path": "app", "name": "app"},
        ]

        for proj in projects:
            (tmp_path / proj["path"]).mkdir(parents=True, exist_ok=True)

        (tmp_path / "core" / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>core</artifactId>
                <version>1.0.0</version>
            </project>
        """))

        (tmp_path / "app" / "pom.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion>
                <groupId>com.example</groupId>
                <artifactId>app</artifactId>
                <version>1.0.0</version>
                <dependencies>
                    <dependency>
                        <groupId>com.example</groupId>
                        <artifactId>core</artifactId>
                        <version>1.0.0</version>
                    </dependency>
                </dependencies>
            </project>
        """))

        graph = WorkspaceGraph(str(tmp_path), projects)
        order = graph.topological_order()
        assert order.index("core") < order.index("app")
        assert graph.dep_count("app") == 1
        assert graph.dependents("core") == ["app"]


class TestGradleKtsPriority:
    """Gradle KTS takes priority over Groovy and pom.xml."""

    def test_kts_over_groovy(self, tmp_path):
        """If both build.gradle.kts and build.gradle exist, KTS wins."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(textwrap.dedent("""\
            dependencies {
                implementation(project(":from-kts"))
            }
        """))
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':from-groovy')
            }
        """))

        scanner = MavenScanner()
        deps = scanner.scan(str(proj_dir), {"app", "from-kts", "from-groovy"})
        names = {d.name for d in deps}
        assert "from-kts" in names
        assert "from-groovy" not in names


class TestNoManifest:
    """No Gradle or Maven files returns empty."""

    def test_no_files(self, tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        scanner = MavenScanner()
        assert scanner.scan(str(proj_dir), {"app"}) == []


class TestGradleFormsThatDeclareNoWorkspaceEdge:
    """Lines that plainly declare no intra-workspace edge are recognized.

    The unrecognized-declaration refusal is a hard error, so every form it
    fires on must really be a declaration the scanner failed to parse.  A
    comment, a BOM import, the ``kotlin("stdlib")`` helper, a local-jar
    ``fileTree``/``files`` and a version-catalog bundle reference are none of
    them: each is recognized here and contributes no edge, exactly as it
    contributes none in Gradle.

    Every case carries a real ``project(...)`` edge beside the ignorable line,
    so a fix that silenced the refusal by dropping the file's parsing
    altogether would fail these too.
    """

    GROOVY_CASES = [
        ("a line comment mentioning a config name", "// api docs: see https://example.com"),
        ("a line comment about an omitted config", "// implementation is intentionally omitted here"),
        ("a commented-out declaration", "// implementation(deps.other)"),
        ("a trailing line comment", "implementation 'com.example:external:1.0' // api deps.other"),
        ("a bom import", "implementation platform('com.example:bom:1.0')"),
        ("a local jar tree", "implementation fileTree(dir: 'libs', include: ['*.jar'])"),
        ("a local jar list", "implementation files('libs/legacy.jar')"),
        ("a catalog bundle reference", "implementation libs.bundles.networking"),
        ("a parenthesized catalog bundle reference", "implementation(libs.bundles.networking)"),
    ]

    KTS_CASES = [
        ("a line comment mentioning a config name", "// api docs: see https://example.com"),
        ("a line comment about an omitted config", "// implementation is intentionally omitted here"),
        ("a commented-out declaration", "// implementation(deps.other)"),
        ("a bom import", 'implementation(platform("com.example:bom:1.0"))'),
        ("a kotlin helper", 'implementation(kotlin("stdlib"))'),
        ("a local jar list", 'implementation(files("libs/legacy.jar"))'),
        ("a catalog bundle reference", "implementation(libs.bundles.networking)"),
    ]

    @pytest.mark.parametrize(
        "line", [case[1] for case in GROOVY_CASES], ids=[case[0] for case in GROOVY_CASES]
    )
    def test_groovy(self, tmp_path, line):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(
            "dependencies {\n"
            "    implementation project(':core')\n"
            f"    {line}\n"
            "}\n"
        )

        deps = MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert [d.name for d in deps] == ["core"]

    @pytest.mark.parametrize(
        "line", [case[1] for case in KTS_CASES], ids=[case[0] for case in KTS_CASES]
    )
    def test_kts(self, tmp_path, line):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(
            "dependencies {\n"
            '    implementation(project(":core"))\n'
            f"    {line}\n"
            "}\n"
        )

        deps = MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert [d.name for d in deps] == ["core"]

    def test_a_block_comment_hides_nothing_and_declares_nothing(self, tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            /*
             * implementation deps.someLib
             * api deps.other
             */
            dependencies {
                implementation project(':core')
            }
        """))

        deps = MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert [d.name for d in deps] == ["core"]

    def test_a_url_in_a_string_is_not_a_comment(self, tmp_path):
        """The comment stripper is quote-aware: ``//`` inside a literal stays."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            repositories {
                maven { url 'https://repo.example.com/m2' }
            }
            dependencies {
                implementation project(':core')
            }
        """))

        deps = MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert [d.name for d in deps] == ["core"]

    def test_a_declaration_the_scanner_really_cannot_read_still_refuses(self, tmp_path):
        """The refusal is narrowed, not removed."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation project(':core')
                implementation deps.someLib
            }
        """))

        with pytest.raises(UnrecognizedGradleDependencyError) as exc:
            MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert exc.value.lines == [(3, "implementation deps.someLib")]

    def test_the_reported_line_keeps_its_comment(self, tmp_path):
        """Comments are ignored for matching, not erased from the report."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation deps.someLib // the variable is defined elsewhere
            }
        """))

        with pytest.raises(UnrecognizedGradleDependencyError) as exc:
            MavenScanner().scan(str(proj_dir), {"app", "core"})
        assert exc.value.lines == [
            (2, "implementation deps.someLib // the variable is defined elsewhere")
        ]


class TestDeclaredDependsOnAcknowledgesAnUnrecognizedLine:
    """A member that states its own edges is not narrowed by an unread line.

    The refusal's whole remedy is ``depends_on``, and a remedy that does not
    clear the error it names leaves the operator with nothing to do but
    rewrite the build file -- which the remedy says is unnecessary.  A member
    whose ``workspace.toml`` entry DECLARES the key (any list, empty
    included) has therefore stated its workspace edges by hand: the scanner
    can no longer narrow them, so the failure stays a warning and is not
    recorded for the consumers that refuse on it.

    Only the unrecognized-declaration class is acknowledged this way.  A
    manifest nobody could READ says nothing about whether the operator's
    declaration is complete for the OTHER scanners that never got to run.
    """

    @staticmethod
    def _gradle_project(tmp_path):
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(textwrap.dedent("""\
            dependencies {
                implementation deps.someLib
            }
        """))
        (tmp_path / "core").mkdir()

    def test_a_declared_depends_on_clears_the_scan_error(self, tmp_path):
        self._gradle_project(tmp_path)
        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app", "depends_on": ["core"]},
            {"path": "core", "name": "core"},
        ])
        assert graph.scan_errors == []
        assert [d.name for d in graph.dependencies("app")] == ["core"]

    def test_an_explicitly_empty_declaration_clears_it_too(self, tmp_path):
        """``depends_on = []`` is the operator saying "no workspace edges"."""
        self._gradle_project(tmp_path)
        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app", "depends_on": []},
            {"path": "core", "name": "core"},
        ])
        assert graph.scan_errors == []
        assert graph.dependencies("app") == []

    def test_without_the_key_it_is_still_recorded(self, tmp_path):
        self._gradle_project(tmp_path)
        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app"},
            {"path": "core", "name": "core"},
        ])
        assert [e.project for e in graph.scan_errors] == ["app"]

    def test_the_warning_reaches_stderr_either_way(self, tmp_path, capsys):
        """Only the refusal is lifted; the stderr warning is informational."""
        self._gradle_project(tmp_path)
        WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app", "depends_on": ["core"]},
            {"path": "core", "name": "core"},
        ])
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "deps.someLib" in captured.err

    def test_an_unreadable_manifest_is_not_acknowledged(self, tmp_path):
        """The acknowledgment covers the unrecognized-pattern class only."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text("this is not valid xml <<<")
        (tmp_path / "core").mkdir()

        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app", "depends_on": ["core"]},
            {"path": "core", "name": "core"},
        ])
        assert [e.project for e in graph.scan_errors] == ["app"]

    def test_the_remedy_says_the_declaration_clears_the_refusal(self, tmp_path):
        self._gradle_project(tmp_path)
        graph = WorkspaceGraph(str(tmp_path), [
            {"path": "app", "name": "app"},
            {"path": "core", "name": "core"},
        ])
        remedy = graph.scan_errors[0].remedy
        assert "depends_on" in remedy
        assert "depends_on = []" in remedy
