"""Tests for JVM import scanners (Java, Kotlin) and Maven read_metadata."""

import os

import pytest

from rlsbl.import_scanners import (
    JavaImportScanner,
    KotlinImportScanner,
    ImportInfo,
    build_jvm_package_map,
)
from rlsbl.targets.maven import MavenTarget


# ---------------------------------------------------------------------------
# Phase 7b: JVM import scanning
# ---------------------------------------------------------------------------


class TestJavaImportScanner:
    """JavaImportScanner detects import statements in .java files."""

    def test_java_import_parsed(self, tmp_path):
        """Standard Java import statement is parsed correctly."""
        src = tmp_path / "src" / "main" / "java"
        src.mkdir(parents=True)
        (src / "App.java").write_text(
            "package com.myapp;\n"
            "\n"
            "import com.example.core.Utils;\n"
            "\n"
            "public class App {}\n"
        )
        scanner = JavaImportScanner()
        package_map = {"com.example.core": "core-lib"}
        results = scanner.scan(
            str(tmp_path), {"core-lib"},
            package_map=package_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "core-lib"
        assert results[0].line_number == 3

    def test_java_static_import(self, tmp_path):
        """Static import (import static ...) is parsed correctly."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "Test.java").write_text(
            "import static com.example.utils.Constants.MAX;\n"
        )
        scanner = JavaImportScanner()
        package_map = {"com.example.utils": "utils-lib"}
        results = scanner.scan(
            str(tmp_path), {"utils-lib"},
            package_map=package_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "utils-lib"

    def test_non_import_lines_ignored(self, tmp_path):
        """Non-import lines (package, class, comments) are ignored."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.java").write_text(
            "package com.myapp;\n"
            "\n"
            "// This is a comment about importing\n"
            "/* import com.example.fake.Stuff; */\n"
            "public class App {\n"
            '    String s = "import com.example.core.X;";\n'
            "}\n"
        )
        scanner = JavaImportScanner()
        package_map = {
            "com.myapp": "myapp",
            "com.example.fake": "fake-lib",
            "com.example.core": "core-lib",
        }
        results = scanner.scan(
            str(tmp_path), {"myapp", "fake-lib", "core-lib"},
            package_map=package_map,
        )
        # Only 'package com.myapp;' matches the regex but it starts with
        # 'package' not 'import', so nothing matches
        assert len(results) == 0

    def test_java_no_package_map_returns_empty(self, tmp_path):
        """Without a package_map, scanner returns empty list."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.java").write_text("import com.example.Foo;\n")
        scanner = JavaImportScanner()
        results = scanner.scan(str(tmp_path), {"example"})
        assert results == []


class TestKotlinImportScanner:
    """KotlinImportScanner detects import statements in .kt/.kts files."""

    def test_kotlin_import_parsed(self, tmp_path):
        """Standard Kotlin import statement is parsed correctly."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "Main.kt").write_text(
            "package com.myapp\n"
            "\n"
            "import com.example.core.Utils\n"
            "\n"
            "fun main() {}\n"
        )
        scanner = KotlinImportScanner()
        package_map = {"com.example.core": "core-lib"}
        results = scanner.scan(
            str(tmp_path), {"core-lib"},
            package_map=package_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "core-lib"
        assert results[0].line_number == 3

    def test_kotlin_import_without_semicolon(self, tmp_path):
        """Kotlin imports without semicolons are parsed."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "Foo.kt").write_text("import com.example.Bar\n")
        scanner = KotlinImportScanner()
        package_map = {"com.example": "example-lib"}
        results = scanner.scan(
            str(tmp_path), {"example-lib"},
            package_map=package_map,
        )
        assert len(results) == 1

    def test_kotlin_kts_file_scanned(self, tmp_path):
        """Kotlin script (.kts) files are scanned."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "build.gradle.kts").write_text(
            "import com.example.plugin.MyPlugin\n"
        )
        scanner = KotlinImportScanner()
        package_map = {"com.example.plugin": "plugin-lib"}
        results = scanner.scan(
            str(tmp_path), {"plugin-lib"},
            package_map=package_map,
        )
        assert len(results) == 1


class TestWildcardImports:
    """Wildcard imports (import com.example.*) are handled correctly."""

    def test_java_wildcard_import(self, tmp_path):
        """Java wildcard import matches the package prefix."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.java").write_text("import com.example.core.*;\n")
        scanner = JavaImportScanner()
        package_map = {"com.example.core": "core-lib"}
        results = scanner.scan(
            str(tmp_path), {"core-lib"},
            package_map=package_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "core-lib"

    def test_kotlin_wildcard_import(self, tmp_path):
        """Kotlin wildcard import matches the package prefix."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "Main.kt").write_text("import com.example.utils.*\n")
        scanner = KotlinImportScanner()
        package_map = {"com.example.utils": "utils-lib"}
        results = scanner.scan(
            str(tmp_path), {"utils-lib"},
            package_map=package_map,
        )
        assert len(results) == 1
        assert results[0].package_name == "utils-lib"


class TestNonImportLinesIgnored:
    """Non-import lines are not matched by JVM scanners."""

    def test_package_declaration_ignored(self, tmp_path):
        """Package declarations are not treated as imports."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.java").write_text(
            "package com.example.core;\n"
            "public class App {}\n"
        )
        scanner = JavaImportScanner()
        package_map = {"com.example.core": "core-lib"}
        results = scanner.scan(
            str(tmp_path), {"core-lib"},
            package_map=package_map,
        )
        assert len(results) == 0

    def test_comment_with_import_ignored(self, tmp_path):
        """Comments containing 'import' are not matched."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.kt").write_text(
            "// import com.example.Foo\n"
            "val x = 1\n"
        )
        scanner = KotlinImportScanner()
        package_map = {"com.example": "example-lib"}
        results = scanner.scan(
            str(tmp_path), {"example-lib"},
            package_map=package_map,
        )
        assert len(results) == 0

    def test_string_literal_with_import_ignored(self, tmp_path):
        """String literals containing 'import' are not matched."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.java").write_text(
            'String s = "import com.example.Foo;";\n'
        )
        scanner = JavaImportScanner()
        package_map = {"com.example": "example-lib"}
        results = scanner.scan(
            str(tmp_path), {"example-lib"},
            package_map=package_map,
        )
        assert len(results) == 0


class TestBuildJvmPackageMap:
    """build_jvm_package_map extracts package prefixes from build files."""

    def test_pom_group_artifact_mapping(self, tmp_path):
        """POM groupId.artifactId is used as package prefix."""
        proj_dir = tmp_path / "core"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<project>\n"
            "  <groupId>com.example</groupId>\n"
            "  <artifactId>core</artifactId>\n"
            "  <version>1.0.0</version>\n"
            "</project>\n"
        )
        projects = [{"name": "core-lib", "path": "core"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {"com.example.core": "core-lib"}

    def test_pom_groupid_only(self, tmp_path):
        """POM with only groupId (no artifactId) uses groupId as prefix."""
        proj_dir = tmp_path / "shared"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<project>\n"
            "  <groupId>com.example.shared</groupId>\n"
            "</project>\n"
        )
        projects = [{"name": "shared-lib", "path": "shared"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {"com.example.shared": "shared-lib"}

    def test_gradle_kts_group(self, tmp_path):
        """build.gradle.kts group is used as package prefix."""
        proj_dir = tmp_path / "utils"
        proj_dir.mkdir()
        (proj_dir / "build.gradle.kts").write_text(
            'group = "com.example.utils"\n'
            'version = "1.0.0"\n'
        )
        projects = [{"name": "utils-lib", "path": "utils"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {"com.example.utils": "utils-lib"}

    def test_gradle_groovy_group(self, tmp_path):
        """build.gradle group is used as package prefix."""
        proj_dir = tmp_path / "common"
        proj_dir.mkdir()
        (proj_dir / "build.gradle").write_text(
            "group = 'com.example.common'\n"
            "version = '1.0.0'\n"
        )
        projects = [{"name": "common-lib", "path": "common"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {"com.example.common": "common-lib"}

    def test_pom_takes_priority_over_gradle(self, tmp_path):
        """When both pom.xml and build.gradle exist, pom.xml is used."""
        proj_dir = tmp_path / "dual"
        proj_dir.mkdir()
        (proj_dir / "pom.xml").write_text(
            "<project>\n"
            "  <groupId>com.pom</groupId>\n"
            "  <artifactId>dual</artifactId>\n"
            "</project>\n"
        )
        (proj_dir / "build.gradle").write_text(
            "group = 'com.gradle'\n"
        )
        projects = [{"name": "dual-lib", "path": "dual"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {"com.pom.dual": "dual-lib"}

    def test_no_build_files_returns_empty(self, tmp_path):
        """Project without JVM build files produces no mapping."""
        proj_dir = tmp_path / "pyproj"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text('[project]\nname = "foo"\n')
        projects = [{"name": "pyproj", "path": "pyproj"}]
        result = build_jvm_package_map(projects, str(tmp_path))
        assert result == {}


# ---------------------------------------------------------------------------
# Phase 7c: Maven read_metadata
# ---------------------------------------------------------------------------


class TestMavenReadMetadataPom:
    """read_metadata returns populated dict for POM projects."""

    def test_full_pom_metadata(self, tmp_path):
        """All POM metadata fields are extracted correctly."""
        (tmp_path / "pom.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<project>\n"
            "  <groupId>com.example</groupId>\n"
            "  <artifactId>myapp</artifactId>\n"
            "  <version>1.2.3</version>\n"
            "  <name>My Application</name>\n"
            "  <description>A sample application</description>\n"
            "  <url>https://example.com</url>\n"
            "  <licenses>\n"
            "    <license>\n"
            "      <name>Apache License 2.0</name>\n"
            "    </license>\n"
            "  </licenses>\n"
            "</project>\n"
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["groupId"] == "com.example"
        assert meta["artifactId"] == "myapp"
        assert meta["version"] == "1.2.3"
        assert meta["name"] == "My Application"
        assert meta["description"] == "A sample application"
        assert meta["url"] == "https://example.com"
        assert meta["license"] == "Apache License 2.0"

    def test_pom_with_namespace(self, tmp_path):
        """POM with Maven namespace prefix is parsed correctly."""
        (tmp_path / "pom.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            "  <groupId>org.test</groupId>\n"
            "  <artifactId>lib</artifactId>\n"
            "  <version>0.1.0</version>\n"
            "  <description>A test library</description>\n"
            "</project>\n"
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["groupId"] == "org.test"
        assert meta["description"] == "A test library"

    def test_pom_multiple_licenses(self, tmp_path):
        """Multiple licenses in POM are joined with comma."""
        (tmp_path / "pom.xml").write_text(
            "<project>\n"
            "  <groupId>com.example</groupId>\n"
            "  <licenses>\n"
            "    <license><name>MIT</name></license>\n"
            "    <license><name>Apache-2.0</name></license>\n"
            "  </licenses>\n"
            "</project>\n"
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["license"] == "MIT, Apache-2.0"

    def test_pom_minimal(self, tmp_path):
        """POM with only groupId still returns partial metadata."""
        (tmp_path / "pom.xml").write_text(
            "<project>\n"
            "  <groupId>com.example</groupId>\n"
            "</project>\n"
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["groupId"] == "com.example"
        assert "description" not in meta


class TestMavenReadMetadataGradle:
    """read_metadata returns populated dict for Gradle projects."""

    def test_gradle_kts_metadata(self, tmp_path):
        """Kotlin DSL build file metadata is extracted."""
        (tmp_path / "build.gradle.kts").write_text(
            'group = "com.example"\n'
            'version = "2.0.0"\n'
            'description = "A Kotlin project"\n'
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["group"] == "com.example"
        assert meta["version"] == "2.0.0"
        assert meta["description"] == "A Kotlin project"

    def test_gradle_groovy_metadata(self, tmp_path):
        """Groovy DSL build file metadata is extracted."""
        (tmp_path / "build.gradle").write_text(
            "group = 'com.example'\n"
            "version = '1.5.0'\n"
            "description = 'A Groovy project'\n"
        )
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta["group"] == "com.example"
        assert meta["version"] == "1.5.0"
        assert meta["description"] == "A Groovy project"

    def test_no_build_files_returns_empty(self, tmp_path):
        """Project without any JVM build file returns empty dict."""
        target = MavenTarget()
        meta = target.read_metadata(str(tmp_path))
        assert meta == {}


class TestMavenCapabilities:
    """read_metadata is in the capabilities frozenset."""

    def test_read_metadata_in_capabilities(self):
        """MavenTarget.capabilities includes 'read_metadata'."""
        assert "read_metadata" in MavenTarget.capabilities
