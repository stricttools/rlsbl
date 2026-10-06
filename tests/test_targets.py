"""Tests for class-based release targets conforming to the ReleaseTarget Protocol."""

import json
import os
import tempfile
from pathlib import Path

import pytest

# The axis -> derived-property map has ONE definition, in the file dedicated to
# the axis derivations. It used to be restated here as CAPABILITY_AXES, free to
# drift from the copy it duplicated.
from test_target_capability_derivation import AXIS_PROPERTIES

from rlsbl.context import ProjectContext
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets.npm import NpmTarget
from rlsbl.targets.pypi import PypiTarget
from conftest import make_ctx
from rlsbl.targets.go import GoTarget
from rlsbl.targets.spec import SpecTarget
from rlsbl.errors import ConfigError
from rlsbl.targets import TARGETS, detect_targets


def _ctx(project_root=".", config=None):
    """Build a ProjectContext for tests with no config by default."""
    if config is None:
        config = {}
    return ProjectContext(project_root=Path(str(project_root)), workspace_root=None, config=config)


class TestNpmTarget:
    def test_is_release_target(self):
        target = NpmTarget()
        assert isinstance(target, ReleaseTarget)

    def test_name(self):
        target = NpmTarget()
        assert target.name == "npm"

    def test_version_file(self):
        target = NpmTarget()
        assert target.version_file() == "package.json"

    def test_detect_true(self):
        target = NpmTarget()
        with tempfile.TemporaryDirectory() as d:
            pkg_path = os.path.join(d, "package.json")
            with open(pkg_path, "w") as f:
                json.dump({"name": "test", "version": "1.0.0"}, f)
            assert target.detect(d) is True

    def test_detect_false(self):
        target = NpmTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_tag_format(self):
        target = NpmTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"


class TestPypiTarget:
    def test_is_release_target(self):
        target = PypiTarget()
        assert isinstance(target, ReleaseTarget)

    def test_name(self):
        target = PypiTarget()
        assert target.name == "pypi"

    def test_version_file(self):
        target = PypiTarget()
        assert target.version_file() == "pyproject.toml"

    def test_detect_true(self):
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "test"\nversion = "1.0.0"\n')
            assert target.detect(d) is True

    def test_detect_false(self):
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_tag_format(self):
        target = PypiTarget()
        assert target.tag_format("2.0.0") == "v2.0.0"


class TestPypiWriteVersion:
    """Tests for PypiTarget.write_version() with tomlkit."""

    def test_write_version_with_project_urls_subtable(self):
        """write_version correctly updates version when [project.urls] sub-table is present."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            content = (
                '[project]\n'
                'name = "my-pkg"\n'
                'version = "1.0.0"\n'
                '\n'
                '[project.urls]\n'
                'Repository = "https://github.com/user/repo"\n'
                '\n'
                '[build-system]\n'
                'requires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            )
            with open(toml_path, "w") as f:
                f.write(content)
            target.write_version(d, "2.0.0", ctx=_ctx())
            with open(toml_path, "r") as f:
                updated = f.read()
            assert 'version = "2.0.0"' in updated
            # Ensure [project.urls] is preserved
            assert '[project.urls]' in updated
            assert 'https://github.com/user/repo' in updated
            # Ensure [build-system] is preserved
            assert '[build-system]' in updated

    def test_write_version_preserves_comments(self):
        """write_version preserves inline comments and formatting."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            content = (
                '[project]\n'
                'name = "my-pkg"  # package name\n'
                'version = "1.0.0"\n'
                'description = "A test package"\n'
            )
            with open(toml_path, "w") as f:
                f.write(content)
            target.write_version(d, "3.5.0", ctx=_ctx())
            with open(toml_path, "r") as f:
                updated = f.read()
            assert 'version = "3.5.0"' in updated
            assert '# package name' in updated


class TestPypiWriteVersionDunderVersion:
    """Tests for PypiTarget.write_version() updating __version__ in package source."""

    def test_updates_dunder_version_in_package_init(self):
        """__version__ in pkg/__init__.py is updated after write_version."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__ = "1.0.0"\n')
            target.write_version(d, "2.0.0", ctx=_ctx())
            with open(init_path) as f:
                content = f.read()
            assert '__version__ = "2.0.0"' in content

    def test_updates_dunder_version_src_layout(self):
        """__version__ in src/pkg/__init__.py is updated (src layout)."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "src", "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write("__version__ = '1.0.0'\n")
            target.write_version(d, "3.0.0", ctx=_ctx())
            with open(init_path) as f:
                content = f.read()
            assert "__version__ = '3.0.0'" in content

    def test_no_init_py_no_error(self):
        """No __init__.py -- no error, version still written to pyproject.toml."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            target.write_version(d, "2.0.0", ctx=_ctx())
            with open(toml_path) as f:
                content = f.read()
            assert 'version = "2.0.0"' in content

    def test_init_py_without_dunder_version_unchanged(self):
        """__init__.py without __version__ -- no error, file unchanged."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            original = '"""My package."""\n\nfrom .core import main\n'
            with open(init_path, "w") as f:
                f.write(original)
            target.write_version(d, "2.0.0", ctx=_ctx())
            with open(init_path) as f:
                content = f.read()
            assert content == original

    def test_typed_annotation_dunder_version(self):
        """__version__: str = '1.0.0' (typed annotation) is updated."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__: str = "1.0.0"\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            with open(init_path) as f:
                content = f.read()
            assert '__version__: str = "2.0.0"' in content
            assert os.path.join("my_pkg", "__init__.py") in result

    def test_prerelease_dunder_version(self):
        """__version__ = '1.0.0rc1' (pre-release) is updated."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0rc1"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__ = "1.0.0rc1"\n')
            result = target.write_version(d, "1.0.1", ctx=_ctx())
            with open(init_path) as f:
                content = f.read()
            assert '__version__ = "1.0.1"' in content
            assert os.path.join("my_pkg", "__init__.py") in result

    def test_dynamic_dunder_version_skipped(self):
        """__version__ = _detect_version() (dynamic) is not modified."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__ = _detect_version()\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml"]
            with open(init_path) as f:
                content = f.read()
            assert content == '__version__ = _detect_version()\n'

    def test_imported_dunder_version_skipped(self):
        """from ._version import __version__ is not modified."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('from ._version import __version__\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml"]
            with open(init_path) as f:
                content = f.read()
            assert content == 'from ._version import __version__\n'


class TestFindDunderVersionNode:
    """Tests for find_dunder_version_node() AST helper."""

    def test_static_literal_returns_node(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('__version__ = "1.0.0"\n')
        assert node is not None
        assert node.value == "1.0.0"

    def test_typed_annotation_returns_node(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('__version__: str = "2.5.0"\n')
        assert node is not None
        assert node.value == "2.5.0"

    def test_dynamic_call_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('__version__ = _detect_version()\n')
        assert node is None

    def test_import_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('from ._version import __version__\n')
        assert node is None

    def test_no_dunder_version_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('x = 42\n')
        assert node is None

    def test_syntax_error_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('def foo(\n')
        assert node is None

    def test_nested_in_function_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('def f():\n    __version__ = "1.0.0"\n')
        assert node is None

    def test_nested_in_if_block_returns_none(self):
        from rlsbl.targets.pypi import find_dunder_version_node
        node = find_dunder_version_node('if True:\n    __version__ = "1.0.0"\n')
        assert node is None


class TestHasAnyDunderVersion:
    """Tests for has_any_dunder_version() AST helper."""

    def test_static_literal_true(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('__version__ = "1.0.0"\n') is True

    def test_dynamic_call_true(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('__version__ = _detect_version()\n') is True

    def test_import_true(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('from ._version import __version__\n') is True

    def test_no_dunder_version_false(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('x = 42\n') is False

    def test_syntax_error_false(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('def foo(\n') is False

    def test_nested_in_function_false(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('def f():\n    __version__ = "1.0.0"\n') is False

    def test_nested_in_if_block_false(self):
        from rlsbl.targets.pypi import has_any_dunder_version
        assert has_any_dunder_version('if True:\n    __version__ = "1.0.0"\n') is False


class TestGoTarget:
    def test_is_release_target(self):
        target = GoTarget()
        assert isinstance(target, ReleaseTarget)

    def test_name(self):
        target = GoTarget()
        assert target.name == "go"

    def test_version_file(self):
        target = GoTarget()
        assert target.version_file() == "VERSION"

    def test_detect_true(self):
        target = GoTarget()
        with tempfile.TemporaryDirectory() as d:
            mod_path = os.path.join(d, "go.mod")
            with open(mod_path, "w") as f:
                f.write("module github.com/user/repo\n\ngo 1.21\n")
            assert target.detect(d) is True

    def test_detect_false(self):
        target = GoTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_tag_format(self):
        target = GoTarget()
        assert target.tag_format("0.5.0") == "v0.5.0"


class TestDetectTargets:
    """Integration tests for detect_targets() discovery function."""

    def test_detect_targets_with_package_json(self):
        """detect_targets('.') in a dir with package.json returns 'npm' in results."""
        with tempfile.TemporaryDirectory() as d:
            pkg_path = os.path.join(d, "package.json")
            with open(pkg_path, "w") as f:
                json.dump({"name": "test-pkg", "version": "1.0.0"}, f)
            result = detect_targets(d)
            result_names = [entry.name for entry in result]
            assert "npm" in result_names

    def test_detect_targets_empty_directory(self):
        """detect_targets('.') in an empty dir returns []."""
        with tempfile.TemporaryDirectory() as d:
            result = detect_targets(d)
            assert result == []


class TestTargetRegistryIntegration:
    """Tests for the TARGETS registry dict and tag_format behavior."""

    def test_tag_format(self):
        """TARGETS['npm'].tag_format('1.2.3') returns 'v1.2.3'."""
        assert TARGETS["npm"].tag_format("1.2.3") == "v1.2.3"

    def test_monorepo_tag_format(self):
        """TARGETS['npm'].monorepo_tag_format('core', '1.2.3') returns 'core@v1.2.3'."""
        assert TARGETS["npm"].monorepo_tag_format("core", "1.2.3") == "core@v1.2.3"

    def test_build_noop(self):
        """TARGETS['npm'].build() is a no-op that doesn't raise."""
        with tempfile.TemporaryDirectory() as d:
            # Should complete without raising
            TARGETS["npm"].build(d, "1.0.0")

    def test_build_accepts_config_kwarg(self):
        """All targets' build() must accept config as a keyword argument."""
        with tempfile.TemporaryDirectory() as d:
            TARGETS["npm"].build(d, "1.0.0", config={"build_timeout": 30})


class _SlowBuildTarget(BaseTarget):
    """A target whose class default build timeout differs from the base's."""

    BUILD_TIMEOUT_DEFAULT = 300


class TestBuildTimeoutResolution:
    """Tests for BaseTarget._resolve_build_timeout: config key > class default.

    There is no environment-variable layer -- see
    tests/test_timeout_config_surface.py, which pins that RLSBL_BUILD_TIMEOUT
    and RLSBL_BUILD_TIMEOUT_<TARGET> are inert.
    """

    def test_level4_class_default(self):
        """With no config, falls back to BUILD_TIMEOUT_DEFAULT."""
        from rlsbl.targets.base import BaseTarget
        t = BaseTarget()
        assert t._resolve_build_timeout(None) == 120

    def test_level4_subclass_override(self):
        """Subclass BUILD_TIMEOUT_DEFAULT overrides the base."""
        t = _SlowBuildTarget()
        assert t._resolve_build_timeout(None) == 300

    def test_level3_config_int(self):
        """Config build_timeout as int overrides class default."""
        from rlsbl.targets.base import BaseTarget
        t = BaseTarget()
        assert t._resolve_build_timeout({"build_timeout": 60}) == 60

    def test_level3_config_dict_target_name(self):
        """Config build_timeout as dict with target name key."""
        t = TARGETS["npm"]
        config = {"build_timeout": {"npm": 45, "default": 90}}
        assert t._resolve_build_timeout(config) == 45

    def test_level3_config_dict_default_fallback(self):
        """Config build_timeout dict falls back to 'default' key."""
        t = TARGETS["npm"]
        config = {"build_timeout": {"pypi": 45, "default": 90}}
        assert t._resolve_build_timeout(config) == 90

    def test_level3_config_dict_no_match(self):
        """Config build_timeout dict with no matching key falls back to class default."""
        t = TARGETS["npm"]
        config = {"build_timeout": {"pypi": 45}}
        assert t._resolve_build_timeout(config) == t.BUILD_TIMEOUT_DEFAULT

    def test_env_vars_are_inert(self, monkeypatch):
        """The deleted env layer must stay deleted: config still wins."""
        t = TARGETS["npm"]
        monkeypatch.setenv("RLSBL_BUILD_TIMEOUT", "200")
        monkeypatch.setenv("RLSBL_BUILD_TIMEOUT_NPM", "30")
        assert t._resolve_build_timeout({"build_timeout": 60}) == 60
        assert t._resolve_build_timeout(None) == t.BUILD_TIMEOUT_DEFAULT

    def test_precedence_order(self):
        """Config key overrides the class default; nothing else participates."""
        t = _SlowBuildTarget()
        assert t._resolve_build_timeout(None) == 300
        assert t._resolve_build_timeout({"build_timeout": 150}) == 150

class TestDetectionFiles:
    """Tests that detection_files is the single source of truth for PROJECT_MANIFESTS."""

    def test_project_manifests_covers_all_detection_files(self):
        """PROJECT_MANIFESTS includes every target's detection_files."""
        from rlsbl.checks import PROJECT_MANIFESTS
        manifests = set(PROJECT_MANIFESTS)
        for name, target in TARGETS.items():
            for f in target.detection_files:
                assert f in manifests, (
                    f"detection_files entry {f!r} from target {name!r} "
                    f"missing in PROJECT_MANIFESTS"
                )

    def test_project_manifests_only_from_detection_files(self):
        """PROJECT_MANIFESTS contains nothing beyond what targets declare."""
        from rlsbl.checks import PROJECT_MANIFESTS
        expected = set()
        for target in TARGETS.values():
            expected.update(target.detection_files)
        assert set(PROJECT_MANIFESTS) == expected

    def test_detection_files_tuple_type(self):
        """Every target's detection_files is a tuple of strings."""
        for name, target in TARGETS.items():
            df = target.detection_files
            assert isinstance(df, tuple), (
                f"{name}.detection_files is {type(df).__name__}, expected tuple"
            )
            for entry in df:
                assert isinstance(entry, str), (
                    f"{name}.detection_files contains {type(entry).__name__}, expected str"
                )


class TestDetectTargetsConfig:
    """Tests for config-driven target detection via .rlsbl/config.json."""

    def test_config_targets_override_autodetection(self):
        """Config targets take precedence: only 'npm' returned even if go.mod exists."""
        with tempfile.TemporaryDirectory() as d:
            # Create go.mod so auto-detection would find 'go'
            with open(os.path.join(d, "go.mod"), "w") as f:
                f.write("module example.com/test\n\ngo 1.21\n")
            # Create config that only declares npm
            rlsbl_dir = os.path.join(d, ".rlsbl")
            os.makedirs(rlsbl_dir)
            with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
                json.dump({"targets": ["npm"]}, f)
            result = detect_targets(d)
            assert [entry.name for entry in result] == ["npm"]

    def test_no_config_falls_back_to_autodetection(self):
        """Without config, detect_targets uses auto-detection (backward compat)."""
        with tempfile.TemporaryDirectory() as d:
            pkg_path = os.path.join(d, "package.json")
            with open(pkg_path, "w") as f:
                json.dump({"name": "test", "version": "1.0.0"}, f)
            result = detect_targets(d)
            result_names = [entry.name for entry in result]
            assert "npm" in result_names

    def test_empty_targets_array_returns_empty(self):
        """Explicit empty targets array means no targets."""
        with tempfile.TemporaryDirectory() as d:
            # Create package.json so auto-detection would find npm
            with open(os.path.join(d, "package.json"), "w") as f:
                json.dump({"name": "test", "version": "1.0.0"}, f)
            # Config explicitly declares no targets
            rlsbl_dir = os.path.join(d, ".rlsbl")
            os.makedirs(rlsbl_dir)
            with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
                json.dump({"targets": []}, f)
            result = detect_targets(d)
            assert result == []

    def test_unknown_target_is_refused(self):
        """An unknown target name is a hard error naming the supported targets."""
        with tempfile.TemporaryDirectory() as d:
            rlsbl_dir = os.path.join(d, ".rlsbl")
            os.makedirs(rlsbl_dir)
            with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
                json.dump({"targets": ["npm", "nonexistent"]}, f)
            with pytest.raises(ConfigError, match="names target 'nonexistent'"):
                detect_targets(d)


class TestSpecTarget:
    def test_protocol_conformance(self):
        assert isinstance(SpecTarget(), ReleaseTarget)

    def test_detection(self, tmp_path):
        assert not SpecTarget().detect(str(tmp_path))
        (tmp_path / "version.json").write_text('{"version": "1.0.0"}')
        assert SpecTarget().detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        target = SpecTarget()
        (tmp_path / "version.json").write_text('{"version": "1.2.3"}')
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "2.0.0"

    def test_tag_format(self):
        assert SpecTarget().tag_format("1.2.3") == "spec-v1.2.3"


class TestGoScaffoldTemplates:
    """Tests for Go scaffold template improvements (goreleaser main, version.go)."""

    def test_goreleaser_main_root(self, tmp_project):
        """Go project with main.go at root returns goreleaserMain: '.'"""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        (tmp_project / "main.go").write_text("package main\n\nfunc main() {}\n")
        (tmp_project / "VERSION").write_text("0.1.0\n")
        vars = target.template_vars(str(tmp_project), make_ctx(tmp_project))
        assert vars["goreleaserMain"] == "."

    def test_goreleaser_main_cmd(self, tmp_project):
        """Go project with cmd/myapp/main.go returns goreleaserMain: './cmd/myapp'"""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        cmd_dir = tmp_project / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
        (tmp_project / "VERSION").write_text("0.1.0\n")
        vars = target.template_vars(str(tmp_project), make_ctx(tmp_project))
        assert vars["goreleaserMain"] == "./cmd/myapp"

    def test_goreleaser_main_empty_for_library(self, tmp_project):
        """Library project (no main packages) gets an empty goreleaserMain --
        libraries never scaffold .goreleaser.yml, and a silent '.' fallback
        used to produce broken configs for misdetected binary projects."""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/mylib\n\ngo 1.21\n")
        (tmp_project / "VERSION").write_text("0.1.0\n")
        vars = target.template_vars(str(tmp_project), make_ctx(tmp_project))
        assert vars["goreleaserMain"] == ""

    def test_version_go_in_binary_mappings(self, tmp_project):
        """Go binary project includes version.go in template_mappings."""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        (tmp_project / "main.go").write_text("package main\n\nfunc main() {}\n")
        mappings = target.template_mappings(ctx=_ctx())
        targets = [m["target"] for m in mappings]
        assert "version.go" in targets

    def test_version_go_skipped_when_var_exists(self, tmp_project):
        """Go binary project with existing version var skips version.go template."""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        (tmp_project / "main.go").write_text(
            "package main\n\nvar Version string\n\nfunc main() {}\n"
        )
        mappings = target.template_mappings(ctx=_ctx())
        targets = [m["target"] for m in mappings]
        assert "version.go" not in targets

    def test_version_go_not_in_library_mappings(self, tmp_project):
        """Go library project does NOT include version.go in template_mappings."""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/mylib\n\ngo 1.21\n")
        (tmp_project / "lib.go").write_text("package mylib\n\nfunc Hello() string { return \"hello\" }\n")
        mappings = target.template_mappings(ctx=_ctx())
        targets = [m["target"] for m in mappings]
        assert "version.go" not in targets


class TestGoLibraryClassification:
    """Tests for GoTarget._is_library() across layouts.

    Replaces tests for the deleted _has_root_main/_has_cmd_main wrappers
    (superseded by go_introspect.list_main_packages, whose per-layout
    enumeration is covered in test_go_introspect.py)."""

    def test_root_main_is_binary(self, tmp_project):
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        (tmp_project / "main.go").write_text("package main\n\nfunc main() {}\n")
        assert target._is_library(str(tmp_project)) is False

    def test_no_main_packages_is_library(self, tmp_project):
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        (tmp_project / "lib.go").write_text("package myapp\n\nfunc Hello() {}\n")
        assert target._is_library(str(tmp_project)) is True

    def test_single_cmd_main_is_binary(self, tmp_project):
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        cmd_dir = tmp_project / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
        assert target._is_library(str(tmp_project)) is False

    def test_multi_binary_is_not_library(self, tmp_project):
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/myapp\n\ngo 1.21\n")
        for name in ("foo", "bar"):
            cmd_dir = tmp_project / "cmd" / name
            cmd_dir.mkdir(parents=True)
            (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
        assert target._is_library(str(tmp_project)) is False


class TestNpmRegistryUrl:
    """Tests for NpmTarget.template_vars() registryUrl from publishConfig."""

    def test_default_registry_url(self, tmp_path):
        """Without publishConfig, registryUrl defaults to https://registry.npmjs.org."""
        target = NpmTarget()
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["registryUrl"] == "https://registry.npmjs.org"

    def test_custom_registry_url(self, tmp_path):
        """publishConfig.registry in package.json overrides the default registryUrl."""
        target = NpmTarget()
        pkg = {
            "name": "test-pkg",
            "version": "1.0.0",
            "publishConfig": {"registry": "https://npm.pkg.github.com"},
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        vars = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars["registryUrl"] == "https://npm.pkg.github.com"


class TestNpmPackageManagerDetection:
    """Tests for NpmTarget._detect_package_manager() and dynamic template selection."""

    def test_detect_npm_from_package_lock(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "package-lock.json").write_text("{}")
        assert target._detect_package_manager(str(tmp_project)) == "npm"

    def test_detect_pnpm_from_lockfile(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "pnpm-lock.yaml").write_text("")
        assert target._detect_package_manager(str(tmp_project)) == "pnpm"

    def test_detect_yarn_from_lockfile(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "yarn.lock").write_text("")
        assert target._detect_package_manager(str(tmp_project)) == "yarn"

    def test_detect_ancestor_search(self, mock_git_repo):
        target = NpmTarget()
        # Lock file at git root
        (mock_git_repo / "pnpm-lock.yaml").write_text("")
        # Subdirectory with package.json
        subdir = mock_git_repo / "packages" / "foo"
        subdir.mkdir(parents=True)
        (subdir / "package.json").write_text('{"name": "foo", "version": "1.0.0"}')
        assert target._detect_package_manager(str(subdir)) == "pnpm"

    def test_detect_fallback_npm(self, tmp_project):
        target = NpmTarget()
        # No lock files anywhere
        assert target._detect_package_manager(str(tmp_project)) == "npm"

    def test_template_mappings_npm(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "package-lock.json").write_text("{}")
        mappings = target.template_mappings(ctx=_ctx())
        ci_templates = [m["template"] for m in mappings if m["target"].endswith("ci.yml")]
        assert ci_templates == ["ci.yml.tpl"]

    def test_template_mappings_pnpm(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "pnpm-lock.yaml").write_text("")
        mappings = target.template_mappings(ctx=_ctx())
        ci_templates = [m["template"] for m in mappings if m["target"].endswith("ci.yml")]
        assert ci_templates == ["ci-pnpm.yml.tpl"]

    def test_template_mappings_yarn(self, tmp_project):
        target = NpmTarget()
        (tmp_project / "yarn.lock").write_text("")
        mappings = target.template_mappings(ctx=_ctx())
        ci_templates = [m["template"] for m in mappings if m["target"].endswith("ci.yml")]
        assert ci_templates == ["ci-yarn.yml.tpl"]


class TestDetectTargetsAutoDetection:
    """Parametrized test that verifies detect_targets() auto-detects every target by its marker file."""

    @pytest.mark.parametrize("target_name,filename,content", [
        ("npm", "package.json", '{"name": "test", "version": "0.1.0"}'),
        ("pypi", "pyproject.toml", '[project]\nname = "test"\nversion = "0.1.0"'),
        ("go", "go.mod", "module example.com/test\n\ngo 1.21"),
        ("spec", "version.json", '{"version": "0.1.0"}'),
    ])
    def test_detect_target_by_marker_file(self, tmp_project, target_name, filename, content):
        marker = tmp_project / filename
        marker.write_text(content)
        result = detect_targets(".")
        result_names = [entry.name for entry in result]
        assert target_name in result_names


class TestWriteVersionReturnPaths:
    """Tests that write_version() returns the list of modified file paths."""

    def test_pypi_returns_both_files_with_dunder_version(self):
        """PypiTarget.write_version returns both pyproject.toml and __init__.py."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__ = "1.0.0"\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml", os.path.join("my_pkg", "__init__.py")]

    def test_pypi_returns_both_files_src_layout(self):
        """PypiTarget.write_version returns src-layout __init__.py path."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "src", "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('__version__ = "1.0.0"\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml", os.path.join("src", "my_pkg", "__init__.py")]

    def test_pypi_returns_only_pyproject_without_init(self):
        """PypiTarget.write_version returns only pyproject.toml when no __init__.py."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml"]

    def test_pypi_returns_only_pyproject_when_init_has_no_dunder(self):
        """PypiTarget.write_version returns only pyproject.toml when __init__.py lacks __version__."""
        target = PypiTarget()
        with tempfile.TemporaryDirectory() as d:
            toml_path = os.path.join(d, "pyproject.toml")
            with open(toml_path, "w") as f:
                f.write('[project]\nname = "my-pkg"\nversion = "1.0.0"\n')
            pkg_dir = os.path.join(d, "my_pkg")
            os.makedirs(pkg_dir)
            init_path = os.path.join(pkg_dir, "__init__.py")
            with open(init_path, "w") as f:
                f.write('"""My package."""\n')
            result = target.write_version(d, "2.0.0", ctx=_ctx())
            assert result == ["pyproject.toml"]

    def test_npm_returns_package_json(self, tmp_path):
        """NpmTarget.write_version returns ['package.json']."""
        target = NpmTarget()
        (tmp_path / "package.json").write_text(json.dumps({"name": "test", "version": "1.0.0"}))
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["package.json"]

    def test_go_returns_version_file(self, tmp_path):
        """GoTarget.write_version returns ['VERSION']."""
        target = GoTarget()
        result = target.write_version(str(tmp_path), "1.0.0", ctx=_ctx())
        assert result == ["VERSION"]

    def test_spec_returns_version_json(self, tmp_path):
        """SpecTarget.write_version returns ['version.json']."""
        target = SpecTarget()
        (tmp_path / "version.json").write_text('{"version": "1.0.0"}')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["version.json"]

    def test_spec_returns_spec_subdir_path(self, tmp_path):
        """SpecTarget.write_version returns relative path when version.json is in spec/."""
        target = SpecTarget()
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "version.json").write_text('{"version": "1.0.0"}')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == [os.path.join("spec", "version.json")]

class TestGoMonorepoTagFormat:
    """Tests for Go monorepo tag format using path-based tags (go/v0.1.1) instead of name-based (name@v0.1.1)."""

    def test_go_monorepo_tag_uses_path(self):
        """GoTarget().monorepo_tag_format with path should return 'go/v0.1.1' for Go modules."""
        result = GoTarget().monorepo_tag_format("go-strictcli", "0.1.1", path="go/")
        assert result == "go/v0.1.1"

    def test_go_monorepo_tag_glob(self):
        """GoTarget().monorepo_tag_glob with path should return 'go/v*' for Go modules."""
        result = GoTarget().monorepo_tag_glob("go-strictcli", path="go/")
        assert result == "go/v*"

    def test_base_monorepo_tag_unchanged(self):
        """NpmTarget base monorepo_tag_format is unchanged: 'mylib@v1.0.0'."""
        result = NpmTarget().monorepo_tag_format("mylib", "1.0.0", path="packages/mylib/")
        assert result == "mylib@v1.0.0"

    def test_go_monorepo_tag_without_path_falls_back(self):
        """GoTarget without path falls back to base name@v format."""
        result = GoTarget().monorepo_tag_format("mylib", "1.0.0")
        assert result == "mylib@v1.0.0"

    def test_go_monorepo_tag_glob_without_path_falls_back(self):
        """GoTarget glob without path falls back to base name@v* format."""
        result = GoTarget().monorepo_tag_glob("mylib")
        assert result == "mylib@v*"

    @pytest.mark.parametrize("target_cls,name,path,version", [
        (GoTarget, "go-strictcli", "go/", "0.1.1"),
        (NpmTarget, "mylib", "packages/mylib/", "1.0.0"),
        (PypiTarget, "mypkg", "python/", "2.3.4"),
    ])
    def test_tag_format_matches_glob_prefix(self, target_cls, name, path, version):
        """For each target, monorepo_tag_format output starts with monorepo_tag_glob prefix (minus trailing *)."""
        target = target_cls()
        tag = target.monorepo_tag_format(name, version, path=path)
        glob = target.monorepo_tag_glob(name, path=path)
        # glob ends with *, the tag should start with the prefix before *
        glob_prefix = glob.rstrip("*")
        assert tag.startswith(glob_prefix), f"tag {tag!r} does not start with glob prefix {glob_prefix!r}"

    def test_go_monorepo_tag_no_trailing_slash_in_path(self):
        """GoTarget().monorepo_tag_format inserts a slash when path lacks one."""
        result = GoTarget().monorepo_tag_format("auth-gateway", "0.1.0", path="auth-gateway")
        assert result == "auth-gateway/v0.1.0"

    def test_go_monorepo_tag_glob_no_trailing_slash_in_path(self):
        """GoTarget().monorepo_tag_glob inserts a slash when path lacks one."""
        result = GoTarget().monorepo_tag_glob("auth-gateway", path="auth-gateway")
        assert result == "auth-gateway/v*"

    def test_go_monorepo_tag_with_trailing_slash_no_double(self):
        """Trailing slash in path must not produce a double slash in the tag."""
        result = GoTarget().monorepo_tag_format("auth-gateway", "0.1.0", path="auth-gateway/")
        assert result == "auth-gateway/v0.1.0"

    def test_go_monorepo_tag_glob_with_trailing_slash_no_double(self):
        """Trailing slash in path must not produce a double slash in the glob."""
        result = GoTarget().monorepo_tag_glob("auth-gateway", path="auth-gateway/")
        assert result == "auth-gateway/v*"


VALID_AUTO_DETECTABLE = {"yes", "no", "conditional"}


def _public_members(cls):
    """Every non-underscore member of *cls*, including bare annotations.

    ``dir()`` misses annotation-only declarations (``ecosystem: str`` on the
    Protocol, ``detection_files`` on the base), which are exactly the members
    a Protocol states without giving a value.
    """
    members = {name for name in dir(cls) if not name.startswith("_")}
    members |= {
        name for name in getattr(cls, "__annotations__", {})
        if not name.startswith("_")
    }
    return members


class TestProtocolCoversTheWholeTargetSurface:
    """``ReleaseTarget`` is the named single authority for the interface.

    It had gone stale: the axis migration added yank, run_tests, the
    dead-module and cycle detectors, the registry-identity methods and every
    derived support property to ``BaseTarget`` without stating any of them on
    the Protocol, so the file that claims to define the interface described
    only part of it.

    The expected set is derived from ``BaseTarget`` rather than hand-listed,
    so a member added there fails this test until the Protocol states it too.
    """

    def test_the_protocol_states_every_public_base_member(self):
        missing = sorted(_public_members(BaseTarget) - _public_members(ReleaseTarget))
        assert not missing, (
            "ReleaseTarget does not declare: " + ", ".join(missing)
            + "\n\nAdd each one to rlsbl/targets/protocol.py with the "
            "signature and docstring it has on BaseTarget."
        )

    def test_every_support_axis_is_stated_on_the_protocol(self):
        """The derived axis properties specifically, named one by one."""
        stated = _public_members(ReleaseTarget)
        for axis, prop in AXIS_PROPERTIES.items():
            assert prop in stated, f"the '{axis}' axis property is not on the Protocol"

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_every_registered_target_satisfies_the_protocol(self, name):
        assert isinstance(TARGETS[name], ReleaseTarget)


class TestTargetIntrospectionConformance:
    """Conformance tests verifying every registered target answers each support axis, ecosystem, and auto_detectable."""

    @pytest.mark.parametrize("name", list(TARGETS.keys()))
    def test_ecosystem_is_nonempty_string(self, name):
        """Every target must declare a non-empty ecosystem string."""
        target = TARGETS[name]
        assert isinstance(target.ecosystem, str), (
            f"{name}.ecosystem is {type(target.ecosystem).__name__}, expected str"
        )
        assert target.ecosystem != "", f"{name}.ecosystem is empty"

    @pytest.mark.parametrize("name", list(TARGETS.keys()))
    def test_every_support_axis_answers_a_bool(self, name):
        """Every axis the deleted ``capabilities`` frozenset encoded is now a
        derived property, and every target must answer all of them.

        The frozenset itself is gone: it was a declaration that could disagree
        with the code it described, and did -- eight targets implemented
        ``read_metadata`` without listing it.
        """
        target = TARGETS[name]
        assert not hasattr(target, "capabilities"), (
            f"{name} still declares a capabilities attribute; the axes are "
            f"derived properties now"
        )
        for axis in AXIS_PROPERTIES.values():
            assert isinstance(getattr(target, axis), bool), (
                f"{name}.{axis} did not answer a bool"
            )

    @pytest.mark.parametrize("name", list(TARGETS.keys()))
    def test_auto_detectable_is_valid(self, name):
        """Every target must declare auto_detectable as one of 'yes', 'no', 'conditional'."""
        target = TARGETS[name]
        assert target.auto_detectable in VALID_AUTO_DETECTABLE, (
            f"{name}.auto_detectable is {target.auto_detectable!r}, "
            f"expected one of {VALID_AUTO_DETECTABLE}"
        )
