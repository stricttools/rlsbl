"""Tests for JVM intra-project analysis: class index, dead module detection, circular deps."""

import os

import pytest

from rlsbl.dep_validation import (
    _build_jvm_class_index,
    find_circular_jvm_deps,
    find_dead_jvm_modules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_file(base, rel_path, content):
    """Write content to a file, creating parent dirs as needed."""
    full = base / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


# ---------------------------------------------------------------------------
# Phase 7.1: _build_jvm_class_index
# ---------------------------------------------------------------------------


class TestBuildJvmClassIndexJava:
    """_build_jvm_class_index with standard Java project layout."""

    def test_single_java_class(self, tmp_path):
        """Single Java class is indexed with correct FQN."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/Foo.java",
            "package com.example;\n\npublic class Foo {}\n",
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "com.example.Foo" in index
        assert index["com.example.Foo"] == os.path.join(
            "src", "main", "java", "com", "example", "Foo.java"
        )

    def test_multiple_types_in_one_file(self, tmp_path):
        """Multiple type declarations in a single Java file are all indexed."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/Types.java",
            (
                "package com.example;\n\n"
                "public class Types {}\n"
                "interface Processor {}\n"
                "enum Status { OK, ERROR }\n"
                "record Point(int x, int y) {}\n"
                "@interface MyAnnotation {}\n"
            ),
        )
        index = _build_jvm_class_index(str(tmp_path))
        expected = {"com.example.Types", "com.example.Processor",
                    "com.example.Status", "com.example.Point",
                    "com.example.MyAnnotation"}
        assert expected.issubset(set(index.keys()))

    def test_modifiers_handled(self, tmp_path):
        """Java classes with various modifiers are indexed correctly."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/Mod.java",
            (
                "package com.example;\n\n"
                "public abstract class Mod {}\n"
                "final class FinalHelper {}\n"
                "sealed class Shape {}\n"
                "static class Inner {}\n"
            ),
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "com.example.Mod" in index
        assert "com.example.FinalHelper" in index
        assert "com.example.Shape" in index

    def test_no_package_declaration(self, tmp_path):
        """Java file without package declaration uses bare type name."""
        _write_file(
            tmp_path,
            "src/main/java/App.java",
            "public class App {}\n",
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "App" in index

    def test_no_src_main_returns_empty(self, tmp_path):
        """Project without src/main/java or src/main/kotlin returns empty."""
        _write_file(tmp_path, "src/Foo.java", "public class Foo {}\n")
        index = _build_jvm_class_index(str(tmp_path))
        assert index == {}


class TestBuildJvmClassIndexKotlin:
    """_build_jvm_class_index with Kotlin source files."""

    def test_single_kotlin_class(self, tmp_path):
        """Single Kotlin class is indexed."""
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Bar.kt",
            "package com.example\n\nclass Bar\n",
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "com.example.Bar" in index

    def test_kotlin_multi_declaration_file(self, tmp_path):
        """Kotlin file with multiple declarations (class, object, interface)."""
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Models.kt",
            (
                "package com.example\n\n"
                "data class User(val name: String)\n"
                "sealed class Event\n"
                "object Singleton\n"
                "interface Repository\n"
                "enum class Color { RED, GREEN, BLUE }\n"
                "value class Email(val value: String)\n"
                "annotation class Api\n"
            ),
        )
        index = _build_jvm_class_index(str(tmp_path))
        expected = {
            "com.example.User", "com.example.Event",
            "com.example.Singleton", "com.example.Repository",
            "com.example.Color", "com.example.Email",
            "com.example.Api",
        }
        assert expected.issubset(set(index.keys()))

    def test_kotlin_modifiers(self, tmp_path):
        """Kotlin classes with various modifiers are indexed."""
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Mods.kt",
            (
                "package com.example\n\n"
                "open class Base\n"
                "abstract class AbstractBase\n"
                "internal class InternalHelper\n"
                "private class PrivateImpl\n"
                "inner class InnerClass\n"
            ),
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "com.example.Base" in index
        assert "com.example.AbstractBase" in index
        assert "com.example.InternalHelper" in index

    def test_mixed_java_and_kotlin(self, tmp_path):
        """Both Java and Kotlin sources are indexed."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/JavaClass.java",
            "package com.example;\npublic class JavaClass {}\n",
        )
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/KotlinClass.kt",
            "package com.example\nclass KotlinClass\n",
        )
        index = _build_jvm_class_index(str(tmp_path))
        assert "com.example.JavaClass" in index
        assert "com.example.KotlinClass" in index


# ---------------------------------------------------------------------------
# Phase 7.2: find_dead_jvm_modules
# ---------------------------------------------------------------------------


def _make_jvm_project(tmp_path, build_file="pom.xml"):
    """Create a minimal JVM project structure with a build file."""
    if build_file == "pom.xml":
        _write_file(
            tmp_path, "pom.xml",
            "<project><groupId>com.example</groupId>"
            "<artifactId>app</artifactId></project>\n",
        )
    elif build_file == "build.gradle.kts":
        _write_file(tmp_path, "build.gradle.kts", 'group = "com.example"\n')
    elif build_file == "build.gradle":
        _write_file(tmp_path, "build.gradle", "group = 'com.example'\n")


class TestFindDeadJvmModules:
    """find_dead_jvm_modules detects unreachable JVM source files."""

    def test_dead_file_detected(self, tmp_path):
        """A file not imported by the entry point is dead."""
        _make_jvm_project(tmp_path)

        # Entry point imports Utils
        _write_file(
            tmp_path,
            "src/main/java/com/example/App.java",
            (
                "package com.example;\n\n"
                "import com.example.Utils;\n\n"
                "public class App {\n"
                "    public static void main(String[] args) {}\n"
                "}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/Utils.java",
            "package com.example;\n\npublic class Utils {}\n",
        )
        # Dead file -- not imported by anyone
        _write_file(
            tmp_path,
            "src/main/java/com/example/Dead.java",
            "package com.example;\n\npublic class Dead {}\n",
        )

        dead = find_dead_jvm_modules(str(tmp_path))
        dead_basenames = [os.path.basename(p) for p in dead]
        assert "Dead.java" in dead_basenames
        assert "App.java" not in dead_basenames
        assert "Utils.java" not in dead_basenames

    def test_everything_reachable(self, tmp_path):
        """When all files are reachable from entry points, no dead modules."""
        _make_jvm_project(tmp_path)

        _write_file(
            tmp_path,
            "src/main/java/com/example/Main.java",
            (
                "package com.example;\n\n"
                "import com.example.Service;\n\n"
                "public class Main {\n"
                "    public static void main(String[] args) {}\n"
                "}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/Service.java",
            (
                "package com.example;\n\n"
                "import com.example.Repository;\n\n"
                "public class Service {}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/Repository.java",
            "package com.example;\n\npublic class Repository {}\n",
        )

        dead = find_dead_jvm_modules(str(tmp_path))
        assert dead == []

    def test_kotlin_entry_point(self, tmp_path):
        """Kotlin top-level fun main() is recognized as an entry point."""
        _make_jvm_project(tmp_path, "build.gradle.kts")

        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Main.kt",
            (
                "package com.example\n\n"
                "import com.example.Helper\n\n"
                "fun main() {\n"
                "    Helper.run()\n"
                "}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Helper.kt",
            "package com.example\n\nobject Helper {\n    fun run() {}\n}\n",
        )
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Unused.kt",
            "package com.example\n\nclass Unused\n",
        )

        dead = find_dead_jvm_modules(str(tmp_path))
        dead_basenames = [os.path.basename(p) for p in dead]
        assert "Unused.kt" in dead_basenames
        assert "Main.kt" not in dead_basenames
        assert "Helper.kt" not in dead_basenames

    def test_no_build_file_returns_empty(self, tmp_path):
        """Project without pom.xml or build.gradle returns empty."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/Foo.java",
            "package com.example;\npublic class Foo {}\n",
        )
        assert find_dead_jvm_modules(str(tmp_path)) == []

    def test_no_entry_points_returns_empty(self, tmp_path):
        """Project with no main methods returns empty (cannot determine reachability)."""
        _make_jvm_project(tmp_path)
        _write_file(
            tmp_path,
            "src/main/java/com/example/Lib.java",
            "package com.example;\npublic class Lib {}\n",
        )
        assert find_dead_jvm_modules(str(tmp_path)) == []

    def test_static_import_resolved(self, tmp_path):
        """Static imports (import static com.example.Foo.BAR) resolve to the class file."""
        _make_jvm_project(tmp_path)

        _write_file(
            tmp_path,
            "src/main/java/com/example/Main.java",
            (
                "package com.example;\n\n"
                "import static com.example.Constants.MAX;\n\n"
                "public class Main {\n"
                "    public static void main(String[] args) {}\n"
                "}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/Constants.java",
            (
                "package com.example;\n\n"
                "public class Constants {\n"
                "    public static final int MAX = 100;\n"
                "}\n"
            ),
        )

        dead = find_dead_jvm_modules(str(tmp_path))
        assert dead == []


# ---------------------------------------------------------------------------
# Phase 7.3: find_circular_jvm_deps
# ---------------------------------------------------------------------------


class TestFindCircularJvmDeps:
    """find_circular_jvm_deps detects circular import cycles."""

    def test_circular_imports_detected(self, tmp_path):
        """Two files that import each other form a cycle."""
        _make_jvm_project(tmp_path)

        _write_file(
            tmp_path,
            "src/main/java/com/example/A.java",
            (
                "package com.example;\n\n"
                "import com.example.B;\n\n"
                "public class A {}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/B.java",
            (
                "package com.example;\n\n"
                "import com.example.A;\n\n"
                "public class B {}\n"
            ),
        )

        cycles = find_circular_jvm_deps(str(tmp_path))
        assert len(cycles) == 1
        cycle_basenames = sorted(os.path.basename(p) for p in cycles[0])
        assert cycle_basenames == ["A.java", "B.java"]

    def test_no_cycles(self, tmp_path):
        """Linear dependency chain has no cycles."""
        _make_jvm_project(tmp_path)

        _write_file(
            tmp_path,
            "src/main/java/com/example/A.java",
            (
                "package com.example;\n\n"
                "import com.example.B;\n\n"
                "public class A {}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/B.java",
            (
                "package com.example;\n\n"
                "import com.example.C;\n\n"
                "public class B {}\n"
            ),
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/C.java",
            "package com.example;\n\npublic class C {}\n",
        )

        cycles = find_circular_jvm_deps(str(tmp_path))
        assert cycles == []

    def test_three_way_cycle(self, tmp_path):
        """A -> B -> C -> A forms a three-way cycle."""
        _make_jvm_project(tmp_path)

        _write_file(
            tmp_path,
            "src/main/java/com/example/A.java",
            "package com.example;\nimport com.example.B;\npublic class A {}\n",
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/B.java",
            "package com.example;\nimport com.example.C;\npublic class B {}\n",
        )
        _write_file(
            tmp_path,
            "src/main/java/com/example/C.java",
            "package com.example;\nimport com.example.A;\npublic class C {}\n",
        )

        cycles = find_circular_jvm_deps(str(tmp_path))
        assert len(cycles) == 1
        assert len(cycles[0]) == 3

    def test_no_build_file_returns_empty(self, tmp_path):
        """Project without build file returns empty."""
        _write_file(
            tmp_path,
            "src/main/java/com/example/A.java",
            "package com.example;\npublic class A {}\n",
        )
        assert find_circular_jvm_deps(str(tmp_path)) == []

    def test_kotlin_circular_deps(self, tmp_path):
        """Kotlin files with circular imports are detected."""
        _make_jvm_project(tmp_path, "build.gradle.kts")

        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/X.kt",
            "package com.example\nimport com.example.Y\nclass X\n",
        )
        _write_file(
            tmp_path,
            "src/main/kotlin/com/example/Y.kt",
            "package com.example\nimport com.example.X\nclass Y\n",
        )

        cycles = find_circular_jvm_deps(str(tmp_path))
        assert len(cycles) == 1
        cycle_basenames = sorted(os.path.basename(p) for p in cycles[0])
        assert cycle_basenames == ["X.kt", "Y.kt"]
