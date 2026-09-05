"""Tests for rlsbl.dep_rewrite -- path dependency detection and rewriting."""

import textwrap

import tomlkit

from rlsbl.dep_rewrite import (
    build_rewrite_map,
    detect_path_deps,
    rewrite_pyproject_deps,
)


class TestDetectPathDeps:
    """detect_path_deps: find path dependencies in pyproject.toml."""

    def test_two_path_deps_one_normal(self, tmp_path):
        """Two path deps and one normal dep: returns only the two path deps."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "core @ {root:uri}/../core",
                "requests>=2.0",
                "utils @ {root:uri}/../utils",
            ]
        """))
        result = detect_path_deps(str(pyproject))
        assert len(result) == 2
        assert result[0]["name"] == "core"
        assert result[0]["original"] == "core @ {root:uri}/../core"
        assert result[0]["line_in_deps"] == 0
        assert result[0]["section"] == "dependencies"
        assert result[1]["name"] == "utils"
        assert result[1]["line_in_deps"] == 2

    def test_no_path_deps(self, tmp_path):
        """pyproject with no path deps: returns empty list."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["requests>=2.0", "click"]
        """))
        result = detect_path_deps(str(pyproject))
        assert result == []

    def test_file_absolute_path_syntax(self, tmp_path):
        """pyproject with file:///absolute/path syntax."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "mylib @ file:///home/user/workspace/mylib",
            ]
        """))
        result = detect_path_deps(str(pyproject))
        assert len(result) == 1
        assert result[0]["name"] == "mylib"
        assert "file:///" in result[0]["original"]

    def test_root_uri_sibling_syntax(self, tmp_path):
        """pyproject with {root:uri}/../sibling syntax."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "sibling @ {root:uri}/../sibling",
            ]
        """))
        result = detect_path_deps(str(pyproject))
        assert len(result) == 1
        assert result[0]["name"] == "sibling"
        assert "{root:uri}" in result[0]["original"]

    def test_optional_dependencies(self, tmp_path):
        """Path deps in optional-dependencies are also detected."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["requests>=2.0"]

            [project.optional-dependencies]
            dev = ["testlib @ {root:uri}/../testlib"]
        """))
        result = detect_path_deps(str(pyproject))
        assert len(result) == 1
        assert result[0]["name"] == "testlib"
        assert result[0]["section"] == "optional-dependencies.dev"

    def test_nonexistent_file(self, tmp_path):
        """Missing file returns empty list."""
        result = detect_path_deps(str(tmp_path / "nonexistent.toml"))
        assert result == []


class TestRewritePyprojectDeps:
    """rewrite_pyproject_deps: replace path deps with versioned constraints."""

    def test_rewrite_one_path_dep(self):
        """Rewrite one path dep, leave normal deps unchanged."""
        content = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "core @ {root:uri}/../core",
                "requests>=2.0",
            ]
        """)
        result = rewrite_pyproject_deps(content, {"core": ">=1.2.0"})
        doc = tomlkit.parse(result)
        deps = list(doc["project"]["dependencies"])
        assert deps[0] == "core>=1.2.0"
        assert deps[1] == "requests>=2.0"

    def test_rewrite_multiple_path_deps(self):
        """Rewrite multiple path deps at once."""
        content = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "core @ {root:uri}/../core",
                "utils @ file:///workspace/utils",
                "requests>=2.0",
            ]
        """)
        rewrites = {"core": ">=1.0.0", "utils": ">=0.3.0"}
        result = rewrite_pyproject_deps(content, rewrites)
        doc = tomlkit.parse(result)
        deps = list(doc["project"]["dependencies"])
        assert deps[0] == "core>=1.0.0"
        assert deps[1] == "utils>=0.3.0"
        assert deps[2] == "requests>=2.0"

    def test_preserve_formatting(self):
        """Comments and formatting are preserved after rewrite."""
        content = textwrap.dedent("""\
            # Project config
            [project]
            name = "app"
            version = "1.0.0"
            # Main deps
            dependencies = [
                "core @ {root:uri}/../core",
                "requests>=2.0",  # HTTP library
            ]
        """)
        result = rewrite_pyproject_deps(content, {"core": ">=1.2.0"})
        # Comments should survive the round-trip
        assert "# Project config" in result
        assert "# HTTP library" in result
        assert "# Main deps" in result
        # The rewrite should have happened
        doc = tomlkit.parse(result)
        deps = list(doc["project"]["dependencies"])
        assert deps[0] == "core>=1.2.0"

    def test_rewrite_optional_dependencies(self):
        """Rewrite path deps in optional-dependencies groups."""
        content = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["requests>=2.0"]

            [project.optional-dependencies]
            dev = ["testlib @ {root:uri}/../testlib", "pytest"]
        """)
        result = rewrite_pyproject_deps(content, {"testlib": ">=0.5.0"})
        doc = tomlkit.parse(result)
        dev_deps = list(doc["project"]["optional-dependencies"]["dev"])
        assert dev_deps[0] == "testlib>=0.5.0"
        assert dev_deps[1] == "pytest"

    def test_empty_rewrites_unchanged(self):
        """Empty rewrites dict: content is returned unchanged."""
        content = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core @ {root:uri}/../core"]
        """)
        result = rewrite_pyproject_deps(content, {})
        assert result == content

    def test_no_project_section(self):
        """Content with no [project] section returns unchanged."""
        content = textwrap.dedent("""\
            [tool.something]
            key = "value"
        """)
        result = rewrite_pyproject_deps(content, {"core": ">=1.0"})
        assert result == content

    def test_unmatched_rewrite_key_ignored(self):
        """Rewrite keys that don't match any dep are silently ignored."""
        content = textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["requests>=2.0"]
        """)
        result = rewrite_pyproject_deps(content, {"nonexistent": ">=1.0"})
        doc = tomlkit.parse(result)
        deps = list(doc["project"]["dependencies"])
        assert deps[0] == "requests>=2.0"


class TestBuildRewriteMap:
    """build_rewrite_map: gather versions from workspace projects."""

    def test_three_projects_with_versions(self, tmp_path):
        """Workspace with 3 pypi projects: map has 3 entries."""
        projects = []
        for name, version in [("alpha", "1.0.0"), ("beta", "2.3.1"), ("gamma", "0.1.0")]:
            proj_dir = tmp_path / "packages" / name
            proj_dir.mkdir(parents=True)
            (proj_dir / "pyproject.toml").write_text(textwrap.dedent(f"""\
                [project]
                name = "{name}"
                version = "{version}"
            """))
            projects.append({"name": name, "path": f"packages/{name}"})

        result = build_rewrite_map(str(tmp_path), projects, graph=None)
        assert result == {
            "alpha": ">=1.0.0",
            "beta": ">=2.3.1",
            "gamma": ">=0.1.0",
        }

    def test_project_no_detectable_target(self, tmp_path):
        """Project with no manifest is excluded from the map."""
        # One project with a pyproject.toml, one without
        alpha_dir = tmp_path / "packages" / "alpha"
        alpha_dir.mkdir(parents=True)
        (alpha_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "alpha"
            version = "1.0.0"
        """))

        bare_dir = tmp_path / "packages" / "bare"
        bare_dir.mkdir(parents=True)
        # No manifest file at all

        projects = [
            {"name": "alpha", "path": "packages/alpha"},
            {"name": "bare", "path": "packages/bare"},
        ]
        result = build_rewrite_map(str(tmp_path), projects, graph=None)
        assert "alpha" in result
        assert "bare" not in result

    def test_project_without_version_key(self, tmp_path):
        """Project whose pyproject.toml has no version key is excluded."""
        proj_dir = tmp_path / "packages" / "noversion"
        proj_dir.mkdir(parents=True)
        (proj_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "noversion"
        """))

        projects = [{"name": "noversion", "path": "packages/noversion"}]
        result = build_rewrite_map(str(tmp_path), projects, graph=None)
        assert result == {}
