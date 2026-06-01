"""Tests for class-based release targets conforming to the ReleaseTarget Protocol."""

import json
import os
import tempfile
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from rlsbl.context import ProjectContext
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets.npm import NpmTarget
from rlsbl.targets.pypi import PypiTarget
from conftest import make_ctx
from rlsbl.targets.go import GoTarget
from rlsbl.targets.swift import SwiftTarget
from rlsbl.targets.swift_apple import SwiftAppleTarget
from rlsbl.targets.spec import SpecTarget
from rlsbl.targets.hex import HexTarget
from rlsbl.targets.deno import DenoTarget
from rlsbl.targets.cargo import CargoTarget
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

    def test_publish_noop(self):
        """TARGETS['go'].publish() without go.mod is effectively a no-op (prints warning)."""
        with tempfile.TemporaryDirectory() as d:
            # Should complete without raising (prints warning about missing go.mod)
            TARGETS["go"].publish(d, "1.0.0", _ctx(d))


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

    def test_unknown_target_warns_and_skips(self, capsys):
        """Unknown target names produce a warning and are skipped."""
        with tempfile.TemporaryDirectory() as d:
            rlsbl_dir = os.path.join(d, ".rlsbl")
            os.makedirs(rlsbl_dir)
            with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
                json.dump({"targets": ["npm", "nonexistent"]}, f)
            result = detect_targets(d)
            assert [entry.name for entry in result] == ["npm"]
            captured = capsys.readouterr()
            assert "nonexistent" in captured.err
            assert "Warning" in captured.err


class TestNpmPublish:
    """Tests for NpmTarget.publish() hybrid behavior."""

    def test_publish_with_token(self, capsys):
        """NpmTarget.publish() calls npm publish when NPM_TOKEN is set."""
        target = NpmTarget()
        with patch.dict(os.environ, {"NPM_TOKEN": "fake-token"}):
            with patch("rlsbl.targets.npm.run") as mock_run:
                target.publish(".", "1.2.3", _ctx())
                mock_run.assert_called_once()
                call_args = mock_run.call_args
                assert call_args[0][0] == "npm"
                assert call_args[0][1] == ["publish", "--provenance", "--access", "public"]
                assert call_args[1]["env"]["NPM_TOKEN"] == "fake-token"
        captured = capsys.readouterr()
        assert "Published to npm: 1.2.3" in captured.out

    def test_publish_without_token(self, capsys):
        """NpmTarget.publish() prints skip message when NPM_TOKEN is absent."""
        target = NpmTarget()
        env = os.environ.copy()
        env.pop("NPM_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            target.publish(".", "1.2.3", _ctx())
        captured = capsys.readouterr()
        assert "Skipping local npm publish (no NPM_TOKEN). CI will handle it." in captured.out


class TestPypiPublish:
    """Tests for PypiTarget.publish() hybrid behavior."""

    def test_publish_with_pypi_token(self, capsys):
        """PypiTarget.publish() calls uv build + uv publish when PYPI_TOKEN is set."""
        target = PypiTarget()
        with patch.dict(os.environ, {"PYPI_TOKEN": "fake-pypi-token"}, clear=False):
            with patch("rlsbl.targets.pypi.run") as mock_run:
                target.publish(".", "2.0.0", _ctx())
                assert mock_run.call_count == 2
                # First call: uv build
                first_call = mock_run.call_args_list[0]
                assert first_call[0][0] == "uv"
                assert first_call[0][1] == ["build"]
                # Second call: uv publish
                second_call = mock_run.call_args_list[1]
                assert second_call[0][0] == "uv"
                assert second_call[0][1] == ["publish"]
                assert second_call[1]["env"]["UV_PUBLISH_TOKEN"] == "fake-pypi-token"
        captured = capsys.readouterr()
        assert "Published to PyPI: 2.0.0" in captured.out

    def test_publish_with_twine_password(self, capsys):
        """PypiTarget.publish() also works with TWINE_PASSWORD as fallback."""
        target = PypiTarget()
        env = os.environ.copy()
        env.pop("PYPI_TOKEN", None)
        env["TWINE_PASSWORD"] = "twine-secret"
        with patch.dict(os.environ, env, clear=True):
            with patch("rlsbl.targets.pypi.run") as mock_run:
                target.publish(".", "2.0.0", _ctx())
                assert mock_run.call_count == 2
                second_call = mock_run.call_args_list[1]
                assert second_call[1]["env"]["UV_PUBLISH_TOKEN"] == "twine-secret"

    def test_publish_without_token(self, capsys):
        """PypiTarget.publish() prints skip message when no token is set."""
        target = PypiTarget()
        env = os.environ.copy()
        env.pop("PYPI_TOKEN", None)
        env.pop("TWINE_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            target.publish(".", "2.0.0", _ctx())
        captured = capsys.readouterr()
        assert (
            "Skipping local PyPI publish (no PYPI_TOKEN or TWINE_PASSWORD). "
            "CI will handle it."
        ) in captured.out


class TestSwiftTarget:
    def test_protocol_conformance(self):
        assert isinstance(SwiftTarget(), ReleaseTarget)

    def test_detection(self, tmp_path):
        # No Package.swift -> not detected
        assert not SwiftTarget().detect(str(tmp_path))
        # With Package.swift -> detected
        (tmp_path / "Package.swift").write_text("// swift package")
        assert SwiftTarget().detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        target = SwiftTarget()
        (tmp_path / "VERSION").write_text("1.2.3\n")
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert (tmp_path / "VERSION").read_text().strip() == "2.0.0"

    def test_tag_format(self):
        assert SwiftTarget().tag_format("1.2.3") == "v1.2.3"


class TestSwiftAppleTarget:
    def test_detect_returns_false(self, tmp_path):
        """SwiftAppleTarget.detect() always returns False, even with Package.swift."""
        target = SwiftAppleTarget()
        (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9")
        assert target.detect(str(tmp_path)) is False

    def test_name(self):
        assert SwiftAppleTarget().name == "swift-apple"

    def test_version_read_write(self, tmp_project):
        target = SwiftAppleTarget()
        (tmp_project / "VERSION").write_text("1.2.3\n")
        assert target.read_version(str(tmp_project)) == "1.2.3"
        target.write_version(str(tmp_project), "2.0.0", ctx=_ctx())
        assert (tmp_project / "VERSION").read_text().strip() == "2.0.0"

    def test_config_based_detection(self, tmp_project):
        """detect_targets returns swift-apple when declared in config."""
        (tmp_project / "Package.swift").write_text("// swift-tools-version:5.9")
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({"targets": ["swift-apple"]}))
        result = detect_targets(".")
        assert [entry.name for entry in result] == ["swift-apple"]

    def test_tag_format(self):
        assert SwiftAppleTarget().tag_format("1.2.3") == "v{version}".format(version="1.2.3")


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


class TestHexTarget:
    def test_protocol_conformance(self):
        assert isinstance(HexTarget(), ReleaseTarget)

    def test_detection(self, tmp_path):
        assert not HexTarget().detect(str(tmp_path))
        (tmp_path / "mix.exs").write_text('defmodule MyApp.MixProject do\n  def project do\n    [app: :myapp, version: "1.0.0"]\n  end\nend')
        assert HexTarget().detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        target = HexTarget()
        content = 'defmodule MyApp.MixProject do\n  def project do\n    [app: :myapp, version: "1.2.3"]\n  end\nend'
        (tmp_path / "mix.exs").write_text(content)
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert '"2.0.0"' in (tmp_path / "mix.exs").read_text()

    def test_tag_format(self):
        assert HexTarget().tag_format("1.2.3") == "v1.2.3"

    def test_publish_with_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEX_API_KEY", "test-key")
        target = HexTarget()
        with unittest.mock.patch("rlsbl.targets.hex.run") as mock_run:
            target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
            mock_run.assert_called_once()

    def test_publish_without_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("HEX_API_KEY", raising=False)
        target = HexTarget()
        target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        captured = capsys.readouterr()
        assert "Skipping local Hex publish (no HEX_API_KEY)" in captured.out


class TestDenoTarget:
    def test_protocol_conformance(self):
        assert isinstance(DenoTarget(), ReleaseTarget)

    def test_detection(self, tmp_path):
        assert not DenoTarget().detect(str(tmp_path))
        (tmp_path / "deno.json").write_text('{"name": "@scope/pkg", "version": "1.0.0"}')
        assert DenoTarget().detect(str(tmp_path))

    def test_detection_jsonc(self, tmp_path):
        (tmp_path / "deno.jsonc").write_text('// config\n{"name": "@scope/pkg", "version": "1.0.0"}')
        assert DenoTarget().detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        target = DenoTarget()
        (tmp_path / "deno.json").write_text('{"name": "@scope/pkg", "version": "1.2.3"}')
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        data = json.loads((tmp_path / "deno.json").read_text())
        assert data["version"] == "2.0.0"

    def test_version_read_jsonc(self, tmp_path):
        target = DenoTarget()
        (tmp_path / "deno.jsonc").write_text('// comment\n{"name": "@scope/pkg", "version": "3.0.0"}')
        assert target.read_version(str(tmp_path)) == "3.0.0"

    def test_version_write_jsonc_preserves_comments(self, tmp_path):
        target = DenoTarget()
        content = '// my config\n{"name": "@scope/pkg", "version": "1.0.0"}'
        (tmp_path / "deno.jsonc").write_text(content)
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        result = (tmp_path / "deno.jsonc").read_text()
        assert "// my config" in result
        assert '"2.0.0"' in result

    def test_tag_format(self):
        assert DenoTarget().tag_format("1.2.3") == "v1.2.3"

    def test_publish_with_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DENO_TOKEN", "test-token")
        target = DenoTarget()
        with unittest.mock.patch("rlsbl.targets.deno.run") as mock_run:
            target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
            mock_run.assert_called_once()

    def test_publish_without_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("DENO_TOKEN", raising=False)
        monkeypatch.delenv("JSR_TOKEN", raising=False)
        target = DenoTarget()
        target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        captured = capsys.readouterr()
        assert "Skipping local Deno publish (no DENO_TOKEN/JSR_TOKEN)" in captured.out


class TestCargoTarget:
    def test_protocol_conformance(self):
        assert isinstance(CargoTarget(), ReleaseTarget)

    def test_detection_with_package(self, tmp_path):
        target = CargoTarget()
        # No Cargo.toml -> not detected
        assert not target.detect(str(tmp_path))
        # With [package] -> detected
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "myapp"\nversion = "1.0.0"\n')
        assert target.detect(str(tmp_path))

    def test_detection_workspace_only(self, tmp_path):
        # Workspace root without [package] -> NOT detected
        (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
        assert not CargoTarget().detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        target = CargoTarget()
        content = '[package]\nname = "myapp"\nversion = "1.2.3"\nedition = "2021"\n'
        (tmp_path / "Cargo.toml").write_text(content)
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        import tomlkit
        doc = tomlkit.parse((tmp_path / "Cargo.toml").read_text())
        assert doc["package"]["version"] == "2.0.0"

    def test_version_preserves_comments(self, tmp_path):
        content = '# My crate\n[package]\nname = "myapp"\nversion = "1.0.0"  # current\nedition = "2021"\n'
        (tmp_path / "Cargo.toml").write_text(content)
        CargoTarget().write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        result = (tmp_path / "Cargo.toml").read_text()
        assert "# My crate" in result
        assert "# current" in result or "2.0.0" in result

    def test_tag_format(self):
        assert CargoTarget().tag_format("1.2.3") == "v1.2.3"

    def test_publish_with_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "test-token")
        target = CargoTarget()
        with unittest.mock.patch("rlsbl.targets.cargo.run") as mock_run:
            target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
            mock_run.assert_called_once()

    def test_publish_without_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("CARGO_REGISTRY_TOKEN", raising=False)
        CargoTarget().publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        assert "Skipping" in capsys.readouterr().out

    def test_is_library_with_lib_section(self, tmp_path):
        target = CargoTarget()
        content = '[package]\nname = "mylib"\nversion = "1.0.0"\n\n[lib]\n'
        (tmp_path / "Cargo.toml").write_text(content)
        assert target._is_library(str(tmp_path))

    def test_is_library_no_main_rs(self, tmp_path):
        target = CargoTarget()
        content = '[package]\nname = "mylib"\nversion = "1.0.0"\n'
        (tmp_path / "Cargo.toml").write_text(content)
        os.makedirs(tmp_path / "src")
        (tmp_path / "src" / "lib.rs").write_text("pub fn hello() {}")
        assert target._is_library(str(tmp_path))

    def test_is_not_library_with_main_rs(self, tmp_path):
        target = CargoTarget()
        content = '[package]\nname = "myapp"\nversion = "1.0.0"\n'
        (tmp_path / "Cargo.toml").write_text(content)
        os.makedirs(tmp_path / "src")
        (tmp_path / "src" / "main.rs").write_text("fn main() {}")
        assert not target._is_library(str(tmp_path))


class TestDockerTarget:
    def test_protocol_conformance(self):
        from rlsbl.targets.docker import DockerTarget
        assert isinstance(DockerTarget(), ReleaseTarget)

    def test_detection(self, tmp_path):
        from rlsbl.targets.docker import DockerTarget
        target = DockerTarget()
        assert not target.detect(str(tmp_path))
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
        assert target.detect(str(tmp_path))

    def test_version_read_write(self, tmp_path):
        from rlsbl.targets.docker import DockerTarget
        target = DockerTarget()
        (tmp_path / "VERSION").write_text("1.2.3\n")
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert (tmp_path / "VERSION").read_text().strip() == "2.0.0"

    def test_tag_format(self):
        from rlsbl.targets.docker import DockerTarget
        assert DockerTarget().tag_format("1.2.3") == "v1.2.3"

    def test_publish_without_token(self, tmp_path, monkeypatch, capsys):
        from rlsbl.targets.docker import DockerTarget
        monkeypatch.delenv("DOCKER_USERNAME", raising=False)
        monkeypatch.delenv("DOCKER_PASSWORD", raising=False)
        target = DockerTarget()
        target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        captured = capsys.readouterr()
        assert "Skipping local docker publish (no DOCKER_USERNAME/DOCKER_PASSWORD)" in captured.out

    def test_publish_reads_config(self, tmp_path, monkeypatch):
        from rlsbl.targets.docker import DockerTarget
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DOCKER_USERNAME", "user")
        monkeypatch.setenv("DOCKER_PASSWORD", "pass")
        # No config -> should warn/error about missing docker config
        os.makedirs(tmp_path / ".rlsbl", exist_ok=True)
        (tmp_path / ".rlsbl" / "config.json").write_text('{}')
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
        target = DockerTarget()
        import io, sys
        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)
        target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        output = captured.getvalue()
        # Should mention missing docker config
        assert "docker" in output.lower() or "image" in output.lower() or "config" in output.lower()

    def test_publish_with_config(self, tmp_path, monkeypatch):
        from rlsbl.targets.docker import DockerTarget
        import shutil
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DOCKER_USERNAME", "user")
        monkeypatch.setenv("DOCKER_PASSWORD", "pass")
        os.makedirs(tmp_path / ".rlsbl", exist_ok=True)
        config = {"docker": {"image": "myapp", "registry": "ghcr.io"}}
        (tmp_path / ".rlsbl" / "config.json").write_text(json.dumps(config))
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
        target = DockerTarget()
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/docker"):
            with unittest.mock.patch("rlsbl.targets.docker.run") as mock_run:
                target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path, config=config))
                # Should have called docker build and docker push
                assert mock_run.call_count >= 2


class TestMavenTarget:
    def test_protocol_conformance(self):
        from rlsbl.targets.maven import MavenTarget
        assert isinstance(MavenTarget(), ReleaseTarget)

    def test_detection_gradle_kts(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        assert not MavenTarget().detect(str(tmp_path))
        (tmp_path / "build.gradle.kts").write_text('plugins { id("java") }\nversion = "1.0.0"\n')
        assert MavenTarget().detect(str(tmp_path))

    def test_detection_pom(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        pom = '<project><version>1.0.0</version></project>'
        (tmp_path / "pom.xml").write_text(pom)
        assert MavenTarget().detect(str(tmp_path))

    def test_version_from_gradle_properties(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        (tmp_path / "build.gradle.kts").write_text('plugins { id("java") }')
        (tmp_path / "gradle.properties").write_text("VERSION_NAME=1.2.3\n")
        target = MavenTarget()
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert "VERSION_NAME=2.0.0" in (tmp_path / "gradle.properties").read_text()

    def test_version_from_gradle_kts(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        (tmp_path / "build.gradle.kts").write_text('version = "1.2.3"\n')
        target = MavenTarget()
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert 'version = "2.0.0"' in (tmp_path / "build.gradle.kts").read_text()

    def test_version_from_pom(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        pom = '<?xml version="1.0"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n  <version>1.2.3</version>\n</project>'
        (tmp_path / "pom.xml").write_text(pom)
        target = MavenTarget()
        assert target.read_version(str(tmp_path)) == "1.2.3"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert "2.0.0" in (tmp_path / "pom.xml").read_text()

    def test_version_priority(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        # gradle.properties takes priority over build.gradle.kts
        (tmp_path / "build.gradle.kts").write_text('version = "9.9.9"\n')
        (tmp_path / "gradle.properties").write_text("version=1.0.0\n")
        assert MavenTarget().read_version(str(tmp_path)) == "1.0.0"

    def test_tag_format(self):
        from rlsbl.targets.maven import MavenTarget
        assert MavenTarget().tag_format("1.2.3") == "v1.2.3"

    def test_publish_with_token(self, tmp_path, monkeypatch):
        from rlsbl.targets.maven import MavenTarget
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(tmp_path / "gradlew", 0o755)
        target = MavenTarget()
        with unittest.mock.patch("rlsbl.targets.maven.run") as mock_run:
            target.publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
            mock_run.assert_called_once()

    def test_publish_without_token(self, tmp_path, monkeypatch, capsys):
        from rlsbl.targets.maven import MavenTarget
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        MavenTarget().publish(str(tmp_path), "1.0.0", _ctx(tmp_path))
        captured = capsys.readouterr()
        assert "Skipping local Maven/Gradle publish (no GITHUB_TOKEN)" in captured.out

    def test_version_from_groovy_gradle(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        (tmp_path / "build.gradle").write_text("version = '1.5.0'\n")
        target = MavenTarget()
        assert target.read_version(str(tmp_path)) == "1.5.0"
        target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert "version = '2.0.0'" in (tmp_path / "build.gradle").read_text()

    def test_detection_groovy_gradle(self, tmp_path):
        from rlsbl.targets.maven import MavenTarget
        (tmp_path / "build.gradle").write_text("apply plugin: 'java'\n")
        assert MavenTarget().detect(str(tmp_path))


import pytest


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

    def test_goreleaser_main_fallback(self, tmp_project):
        """Go project with no main.go anywhere returns goreleaserMain: '.'"""
        target = GoTarget()
        (tmp_project / "go.mod").write_text("module github.com/user/mylib\n\ngo 1.21\n")
        (tmp_project / "VERSION").write_text("0.1.0\n")
        vars = target.template_vars(str(tmp_project), make_ctx(tmp_project))
        assert vars["goreleaserMain"] == "."

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


class TestGoRootMainDetection:
    """Tests for GoTarget._has_root_main() and _has_cmd_main() detection."""

    def test_has_root_main_true(self, tmp_project):
        target = GoTarget()
        (tmp_project / "main.go").write_text("package main\n\nfunc main() {}\n")
        assert target._has_root_main(str(tmp_project)) is True

    def test_has_root_main_false(self, tmp_project):
        target = GoTarget()
        # No main.go at root
        assert target._has_root_main(str(tmp_project)) is False

    def test_has_cmd_main_single_binary(self, tmp_project):
        target = GoTarget()
        cmd_dir = tmp_project / "cmd" / "myapp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
        assert target._has_cmd_main(str(tmp_project)) is True

    def test_has_cmd_main_multi_binary(self, tmp_project):
        target = GoTarget()
        for name in ("foo", "bar"):
            cmd_dir = tmp_project / "cmd" / name
            cmd_dir.mkdir(parents=True)
            (cmd_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
        # Multi-binary: cmd/ is correct, should return False
        assert target._has_cmd_main(str(tmp_project)) is False

    def test_has_cmd_main_no_cmd(self, tmp_project):
        target = GoTarget()
        # No cmd/ directory at all
        assert target._has_cmd_main(str(tmp_project)) is False


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
    """Parametrized test that verifies detect_targets() auto-detects all 11 auto-detectable targets."""

    @pytest.mark.parametrize("target_name,filename,content", [
        ("npm", "package.json", '{"name": "test", "version": "0.1.0"}'),
        ("pypi", "pyproject.toml", '[project]\nname = "test"\nversion = "0.1.0"'),
        ("go", "go.mod", "module example.com/test\n\ngo 1.21"),
        ("swift", "Package.swift", "// swift-tools-version:5.9"),
        ("cargo", "Cargo.toml", '[package]\nname = "test"\nversion = "0.1.0"'),
        ("deno", "deno.json", '{"name": "test", "version": "0.1.0"}'),
        ("docker", "Dockerfile", "FROM alpine"),
        ("hex", "mix.exs", "defmodule Test.MixProject do"),
        ("maven", "pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>com.test</groupId><artifactId>test</artifactId><version>0.1.0</version></project>"),
        ("spec", "version.json", '{"version": "0.1.0"}'),
        ("docs", "selfdoc.json", "{}"),
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

    def test_zig_returns_both_files_with_zon(self):
        """ZigTarget.write_version returns VERSION and build.zig.zon when zon exists."""
        from rlsbl.targets.zig import ZigTarget
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            zon_content = '.{\n    .name = "my-project",\n    .version = "0.1.0",\n}\n'
            with open(os.path.join(d, "build.zig.zon"), "w") as f:
                f.write(zon_content)
            result = target.write_version(d, "1.0.0", ctx=_ctx())
            assert result == ["VERSION", "build.zig.zon"]

    def test_zig_returns_only_version_without_zon(self):
        """ZigTarget.write_version returns only VERSION when no build.zig.zon."""
        from rlsbl.targets.zig import ZigTarget
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            result = target.write_version(d, "1.0.0", ctx=_ctx())
            assert result == ["VERSION"]

    def test_maven_returns_gradle_properties(self, tmp_path):
        """MavenTarget.write_version returns the gradle.properties path."""
        from rlsbl.targets.maven import MavenTarget
        target = MavenTarget()
        (tmp_path / "build.gradle.kts").write_text('plugins { id("java") }')
        (tmp_path / "gradle.properties").write_text("VERSION_NAME=1.0.0\n")
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["gradle.properties"]

    def test_maven_returns_pom_xml(self, tmp_path):
        """MavenTarget.write_version returns pom.xml when that is the version source."""
        from rlsbl.targets.maven import MavenTarget
        pom = '<project><version>1.0.0</version></project>'
        (tmp_path / "pom.xml").write_text(pom)
        result = MavenTarget().write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["pom.xml"]

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

    def test_cargo_returns_cargo_toml(self, tmp_path):
        """CargoTarget.write_version returns ['Cargo.toml']."""
        target = CargoTarget()
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\nversion = "1.0.0"\n')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["Cargo.toml"]

    def test_deno_returns_deno_json(self, tmp_path):
        """DenoTarget.write_version returns ['deno.json']."""
        target = DenoTarget()
        (tmp_path / "deno.json").write_text('{"name": "test", "version": "1.0.0"}')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["deno.json"]

    def test_deno_returns_deno_jsonc(self, tmp_path):
        """DenoTarget.write_version returns ['deno.jsonc'] for jsonc files."""
        target = DenoTarget()
        (tmp_path / "deno.jsonc").write_text('// comment\n{"name": "test", "version": "1.0.0"}')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["deno.jsonc"]

    def test_hex_returns_mix_exs(self, tmp_path):
        """HexTarget.write_version returns ['mix.exs']."""
        target = HexTarget()
        (tmp_path / "mix.exs").write_text('defmodule T do\n  [version: "1.0.0"]\nend')
        result = target.write_version(str(tmp_path), "2.0.0", ctx=_ctx())
        assert result == ["mix.exs"]

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

    def test_docs_returns_selfdoc_json(self):
        """DocsTarget.write_version returns ['selfdoc.json']."""
        from rlsbl.targets.docs import DocsTarget
        target = DocsTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                json.dump({"language": "python"}, f, indent=2)
            result = target.write_version(d, "1.0.0", ctx=_ctx())
            assert result == ["selfdoc.json"]

    def test_plain_returns_version_file(self, tmp_path):
        """PlainTarget.write_version returns ['VERSION']."""
        from rlsbl.targets.plain import PlainTarget
        target = PlainTarget()
        result = target.write_version(str(tmp_path), "1.0.0", ctx=_ctx())
        assert result == ["VERSION"]

    def test_swift_returns_version_file(self, tmp_path):
        """SwiftTarget.write_version returns ['VERSION']."""
        target = SwiftTarget()
        result = target.write_version(str(tmp_path), "1.0.0", ctx=_ctx())
        assert result == ["VERSION"]

    def test_swift_apple_returns_version_file(self, tmp_path):
        """SwiftAppleTarget.write_version returns ['VERSION']."""
        target = SwiftAppleTarget()
        result = target.write_version(str(tmp_path), "1.0.0", ctx=_ctx())
        assert result == ["VERSION"]


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
