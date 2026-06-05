"""Tests for PlainTarget: VERSION file handling and opt-in-only detection."""

import os
import tempfile

from conftest import make_ctx
from rlsbl.targets.plain import PlainTarget
from rlsbl.targets import TARGETS


class TestPlainTargetDetect:
    """PlainTarget.detect() returns True when VERSION exists and no other target manifests are present."""

    def test_detect_false_empty_dir(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_true_version_only(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            assert target.detect(d) is True

    def test_detect_false_with_package_json(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "package.json"), "w") as f:
                f.write("{}")
            assert target.detect(d) is False

    def test_detect_false_with_pyproject_toml(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write("[project]\n")
            assert target.detect(d) is False

    def test_detect_false_with_go_mod(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "go.mod"), "w") as f:
                f.write("module example\n")
            assert target.detect(d) is False

    def test_detect_false_with_cargo_toml(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "Cargo.toml"), "w") as f:
                f.write("[package]\n")
            assert target.detect(d) is False

    def test_detect_false_no_version_file(self):
        """Other manifests present but no VERSION file -- still False."""
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "README.md"), "w") as f:
                f.write("# Hello\n")
            assert target.detect(d) is False

    def test_detect_false_with_selfdoc_json(self):
        """VERSION + selfdoc.json means docs target, not plain."""
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "selfdoc.json"), "w") as f:
                f.write("{}")
            assert target.detect(d) is False

    def test_detect_true_version_with_unrelated_files(self):
        """VERSION exists with non-manifest files -- should detect."""
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "README.md"), "w") as f:
                f.write("# Hello\n")
            os.makedirs(os.path.join(d, "src"))
            assert target.detect(d) is True


class TestPlainTargetReadVersion:
    """Reading version from VERSION file."""

    def test_read_version(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("2.3.4\n")
            assert target.read_version(d) == "2.3.4"

    def test_read_version_strips_whitespace(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("  1.0.0  \n")
            assert target.read_version(d) == "1.0.0"

    def test_read_version_raises_when_no_file(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            try:
                target.read_version(d)
                assert False, "Expected FileNotFoundError"
            except FileNotFoundError:
                pass


class TestPlainTargetWriteVersion:
    """Writing version to VERSION file atomically."""

    def test_write_version_creates_file(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            target.write_version(d, "1.0.0", ctx=make_ctx(d))
            path = os.path.join(d, "VERSION")
            assert os.path.exists(path)
            with open(path) as f:
                assert f.read() == "1.0.0\n"

    def test_write_version_updates_existing(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            target.write_version(d, "2.0.0", ctx=make_ctx(d))
            with open(os.path.join(d, "VERSION")) as f:
                assert f.read() == "2.0.0\n"

    def test_write_version_no_tmp_left_behind(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            target.write_version(d, "1.0.0", ctx=make_ctx(d))
            files = os.listdir(d)
            assert "VERSION.tmp" not in files


class TestPlainTargetWriteVersionPyproject:
    """Writing version bumps pyproject.toml when present with [project].version."""

    def test_write_version_bumps_pyproject_toml(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            # Create VERSION
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            # Create pyproject.toml with [project].version
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "test"\nversion = "1.0.0"\n')
            modified = target.write_version(d, "2.0.0", ctx=make_ctx(d))
            assert "VERSION" in modified
            assert "pyproject.toml" in modified
            # Check VERSION was updated
            with open(os.path.join(d, "VERSION")) as f:
                assert f.read() == "2.0.0\n"
            # Check pyproject.toml was updated
            import tomlkit
            with open(os.path.join(d, "pyproject.toml")) as f:
                doc = tomlkit.parse(f.read())
            assert doc["project"]["version"] == "2.0.0"

    def test_write_version_without_pyproject_toml(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            modified = target.write_version(d, "1.0.0", ctx=make_ctx(d))
            assert modified == ["VERSION"]
            with open(os.path.join(d, "VERSION")) as f:
                assert f.read() == "1.0.0\n"

    def test_write_version_pyproject_no_project_version(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            # Create VERSION
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            # Create pyproject.toml WITHOUT [project].version
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "test"\n')
            modified = target.write_version(d, "2.0.0", ctx=make_ctx(d))
            assert modified == ["VERSION"]
            # pyproject.toml should be unchanged
            with open(os.path.join(d, "pyproject.toml")) as f:
                content = f.read()
            assert "version" not in content.lower() or "2.0.0" not in content

    def test_write_version_pyproject_no_tmp_left_behind(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("1.0.0\n")
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "test"\nversion = "1.0.0"\n')
            target.write_version(d, "2.0.0", ctx=make_ctx(d))
            files = os.listdir(d)
            assert "pyproject.toml.tmp" not in files
            assert "VERSION.tmp" not in files


class TestPlainTargetProperties:
    """Static properties and template methods."""

    def test_name(self):
        target = PlainTarget()
        assert target.name == "plain"

    def test_version_file(self):
        target = PlainTarget()
        assert target.version_file() == "VERSION"

    def test_template_mappings_has_version(self):
        target = PlainTarget()
        mappings = target.template_mappings(ctx=make_ctx("."))
        assert len(mappings) == 1
        assert mappings[0]["template"] == "VERSION.tpl"
        assert mappings[0]["target"] == "VERSION"

    def test_template_dir_exists(self):
        target = PlainTarget()
        d = target.template_dir()
        assert d is not None
        assert os.path.isdir(d)
        assert os.path.isfile(os.path.join(d, "VERSION.tpl"))

    def test_check_project_exists_always_true(self):
        target = PlainTarget()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            assert target.check_project_exists(d) is True

    def test_template_vars_with_version(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("3.1.0\n")
            vars = target.template_vars(d, d)
            assert vars["name"] == os.path.basename(d)
            assert vars["version"] == "3.1.0"

    def test_template_vars_fallback_version(self):
        target = PlainTarget()
        with tempfile.TemporaryDirectory() as d:
            vars = target.template_vars(d, d)
            assert vars["name"] == os.path.basename(d)
            assert vars["version"] == "0.0.0"

    def test_tag_format_inherited(self):
        target = PlainTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"

    def test_monorepo_tag_format_inherited(self):
        target = PlainTarget()
        assert target.monorepo_tag_format("myproject", "1.2.3") == "myproject@v1.2.3"


class TestPlainTargetRegistered:
    """PlainTarget is registered in TARGETS."""

    def test_registered_in_targets(self):
        assert "plain" in TARGETS
        assert isinstance(TARGETS["plain"], PlainTarget)
