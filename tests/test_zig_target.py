"""Tests for ZigTarget: detection, version delegation, template vars, and registration."""

import os
import tempfile

from conftest import make_ctx
from rlsbl.targets.zig import ZigTarget, ZIG_TARGET_MAP, _zig_archive_fn
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets import TARGETS
from rlsbl.npm_wrapper import DEFAULT_PLATFORMS, PlatformSpec


SAMPLE_ZON = """\
.{
    .name = "my-zig-project",
    .version = "0.1.0",
    .minimum_zig_version = "0.13.0",
    .paths = .{
        "build.zig",
        "build.zig.zon",
        "src",
    },
}
"""

SAMPLE_ZON_NO_NAME = """\
.{
    .version = "0.2.0",
    .minimum_zig_version = "0.13.0",
}
"""

SAMPLE_ZON_NO_MIN_ZIG = """\
.{
    .name = "minless",
    .version = "0.3.0",
}
"""

SAMPLE_ZON_MALFORMED = """\
this is not valid zon at all
"""

BUILD_ZIG_BINARY = """\
const std = @import("std");

pub fn build(b: *std.Build) !void {
    const exe = b.addExecutable(.{
        .name = "my-zig-project",
        .root_source_file = b.path("src/main.zig"),
    });
    b.installArtifact(exe);
}
"""

BUILD_ZIG_LIBRARY = """\
const std = @import("std");

pub fn build(b: *std.Build) !void {
    const lib = b.addStaticLibrary(.{
        .name = "my-zig-lib",
        .root_source_file = b.path("src/root.zig"),
    });
    b.installArtifact(lib);
}
"""

BUILD_ZIG_EXE_CALL = """\
const std = @import("std");

pub fn build(b: *std.Build) !void {
    const target = b.standardTargetOptions(.{});
    _ = b.exe("app", "src/main.zig", target);
}
"""


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestZigTargetDetect:
    """ZigTarget.detect() checks for build.zig.zon (primary) and build.zig (secondary)."""

    def test_detect_with_zon(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            assert target.detect(d) is True

    def test_detect_with_build_zig_only(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
            assert target.detect(d) is True

    def test_detect_false_empty_dir(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_prefers_zon(self):
        """Both files present -- detect via build.zig.zon (primary)."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
            assert target.detect(d) is True


class TestZigTargetReadVersion:
    """read_version delegates to read_zig_version."""

    def test_read_from_version_file(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "VERSION"), "1.2.3\n")
            assert target.read_version(d) == "1.2.3"

    def test_read_from_zon_fallback(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            assert target.read_version(d) == "0.1.0"

    def test_raises_when_no_version_source(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            try:
                target.read_version(d)
                assert False, "Expected FileNotFoundError"
            except FileNotFoundError:
                pass


class TestZigTargetWriteVersion:
    """write_version delegates to write_zig_version."""

    def test_writes_version_file(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            target.write_version(d, "2.0.0", ctx=make_ctx(d))
            assert _read(os.path.join(d, "VERSION")) == "2.0.0\n"

    def test_syncs_zon(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            target.write_version(d, "1.5.0", ctx=make_ctx(d))
            content = _read(os.path.join(d, "build.zig.zon"))
            assert '.version = "1.5.0"' in content


class TestZigTargetProperties:
    """Static properties and method return values."""

    def test_name(self):
        target = ZigTarget()
        assert target.name == "zig"

    def test_version_file(self):
        target = ZigTarget()
        assert target.version_file() == "VERSION"

    def test_is_release_target(self):
        target = ZigTarget()
        assert isinstance(target, ReleaseTarget)

    def test_tag_format_inherited(self):
        target = ZigTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"

    def test_template_dir(self):
        target = ZigTarget()
        td = target.template_dir()
        assert td is not None
        assert td.endswith(os.path.join("templates", "zig"))

    def test_get_project_init_hint(self):
        target = ZigTarget()
        assert "zig init" in target.get_project_init_hint()


class TestZigTargetTemplateVars:
    """template_vars extracts name, version, minRequiredZig, isLibrary."""

    def test_extracts_name_from_zon(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["name"] == "my-zig-project"
            assert vars["zig.projectName"] == "my-zig-project"

    def test_extracts_min_required_zig(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.minRequiredZig"] == "0.13.0"

    def test_fallback_name_to_dirname(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["name"] == os.path.basename(d)

    def test_fallback_version(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            vars = target.template_vars(d, make_ctx(d))
            assert vars["version"] == "0.0.0"

    def test_fallback_min_zig_version(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON_NO_MIN_ZIG)
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.minRequiredZig"] == "0.14.0"

    def test_detects_binary(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.isLibrary"] is False

    def test_detects_library(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_LIBRARY)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.isLibrary"] is True

    def test_detects_binary_exe_call(self):
        """exe( shorthand also detects a binary target."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_EXE_CALL)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.isLibrary"] is False

    def test_library_when_no_build_zig(self):
        """No build.zig means library (no executable detected)."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            vars = target.template_vars(d, make_ctx(d))
            assert vars["zig.isLibrary"] is True

    def test_malformed_zon_uses_fallbacks(self):
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON_MALFORMED)
            vars = target.template_vars(d, make_ctx(d))
            assert vars["name"] == os.path.basename(d)
            assert vars["version"] == "0.0.0"
            assert vars["zig.minRequiredZig"] == "0.14.0"


class TestZigTargetTemplateMappings:
    """template_mappings returns expected list for CI/publish workflows."""

    def test_returns_expected_mappings(self):
        target = ZigTarget()
        mappings = target.template_mappings(ctx=make_ctx("."))
        templates = [m["template"] for m in mappings]
        targets = [m["target"] for m in mappings]
        assert "VERSION.tpl" in templates
        assert "ci.yml.tpl" in templates
        # publish.yml.tpl is now owned by the pipeline, not the target
        assert "publish.yml.tpl" not in templates
        assert "VERSION" in targets
        assert ".github/workflows/ci.yml" in targets

    def test_mappings_are_list_of_dicts(self):
        target = ZigTarget()
        mappings = target.template_mappings(ctx=make_ctx("."))
        assert isinstance(mappings, list)
        for m in mappings:
            assert isinstance(m, dict)
            assert "template" in m
            assert "target" in m


class TestZigTargetRegistered:
    """ZigTarget is registered in TARGETS."""

    def test_registered_in_targets(self):
        assert "zig" in TARGETS
        assert isinstance(TARGETS["zig"], ZigTarget)

    def test_auto_detects_zon(self):
        """Auto-detection finds zig target when build.zig.zon is present."""
        from rlsbl.targets import detect_targets
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            found = detect_targets(d)
            names = [t.name for t in found]
            assert "zig" in names


class TestZigArchiveFunction:
    """_zig_archive_fn produces correct asset patterns for all 6 platforms."""

    def test_linux_x64(self):
        spec = PlatformSpec("linux-x64", "linux", "x64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-x86_64-linux"
        assert extract is None
        assert binary == "myapp"

    def test_linux_arm64(self):
        spec = PlatformSpec("linux-arm64", "linux", "arm64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-aarch64-linux"
        assert extract is None
        assert binary == "myapp"

    def test_darwin_x64(self):
        spec = PlatformSpec("darwin-x64", "darwin", "x64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-x86_64-macos"
        assert extract is None
        assert binary == "myapp"

    def test_darwin_arm64(self):
        spec = PlatformSpec("darwin-arm64", "darwin", "arm64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-aarch64-macos"
        assert extract is None
        assert binary == "myapp"

    def test_win32_x64(self):
        spec = PlatformSpec("win32-x64", "win32", "x64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-x86_64-windows.exe"
        assert extract is None
        assert binary == "myapp.exe"

    def test_win32_arm64(self):
        spec = PlatformSpec("win32-arm64", "win32", "arm64")
        asset, extract, binary = _zig_archive_fn(spec, "myapp")
        assert asset == "myapp-aarch64-windows.exe"
        assert extract is None
        assert binary == "myapp.exe"

    def test_all_platforms_covered(self):
        """All 6 DEFAULT_PLATFORMS produce valid artifacts."""
        for spec in DEFAULT_PLATFORMS:
            asset, extract, binary = _zig_archive_fn(spec, "tool")
            assert asset  # non-empty
            assert extract is None  # Zig always produces raw binaries
            assert binary  # non-empty

    def test_zig_target_map_has_all_default_platforms(self):
        """ZIG_TARGET_MAP covers all DEFAULT_PLATFORMS."""
        for spec in DEFAULT_PLATFORMS:
            assert spec.npm_platform in ZIG_TARGET_MAP


class TestZigNpmWrapperTemplateVars:
    """Test npmScope and npmPublishJobs generation for Zig target."""

    def _setup_zig_binary_project(self, d):
        """Create a minimal Zig binary project in directory d."""
        _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
        _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
        _write(os.path.join(d, "VERSION"), "0.1.0\n")

    def _setup_zig_library_project(self, d):
        """Create a minimal Zig library project in directory d."""
        _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
        _write(os.path.join(d, "build.zig"), BUILD_ZIG_LIBRARY)
        _write(os.path.join(d, "VERSION"), "0.1.0\n")

    def test_npm_publish_jobs_empty_without_config(self, monkeypatch):
        """npmPublishJobs is empty when npm_wrapper not configured."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            self._setup_zig_binary_project(d)
            monkeypatch.chdir(d)
            vars_ = target.template_vars(d, make_ctx(d))
            assert vars_.get("npmPublishJobs", "") == ""
            assert vars_.get("npmScope", "") == ""

    def test_npm_scope_with_config(self, monkeypatch):
        """npmScope set when npm_wrapper.scope configured."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            self._setup_zig_binary_project(d)
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(
                os.path.join(config_dir, "config.json"),
                '{"npm_wrapper": {"scope": "@ziguser"}}',
            )
            monkeypatch.chdir(d)
            vars_ = target.template_vars(d, make_ctx(d))
            assert vars_["npmScope"] == "@ziguser"

    def test_npm_publish_jobs_with_config(self, monkeypatch):
        """npmPublishJobs generated when npm_wrapper.scope configured."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            self._setup_zig_binary_project(d)
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(
                os.path.join(config_dir, "config.json"),
                '{"npm_wrapper": {"scope": "@ziguser"}}',
            )
            monkeypatch.chdir(d)
            vars_ = target.template_vars(d, make_ctx(d))
            jobs = vars_.get("npmPublishJobs", "")
            assert "npm publish" in jobs
            assert "npm-publish:" in jobs
            # Zig uses cp (raw binaries), not tar/unzip
            assert "cp my-zig-project-x86_64-linux npm-wrapper/linux-x64/" in jobs
            assert "cp my-zig-project-aarch64-macos npm-wrapper/darwin-arm64/" in jobs
            assert "cp my-zig-project-x86_64-windows.exe npm-wrapper/win32-x64/" in jobs

    def test_library_no_npm_wrapper_even_with_config(self, monkeypatch):
        """Library projects don't get npm wrapper even with config."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            self._setup_zig_library_project(d)
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(
                os.path.join(config_dir, "config.json"),
                '{"npm_wrapper": {"scope": "@ziguser"}}',
            )
            monkeypatch.chdir(d)
            vars_ = target.template_vars(d, make_ctx(d))
            assert vars_.get("npmPublishJobs", "") == ""


class TestZigNpmWrapperTemplateMappings:
    """Test shared_template_mappings includes npm wrapper when configured."""

    def test_mappings_include_npm_wrapper_when_configured(self, monkeypatch):
        """npm wrapper mappings included in shared_template_mappings when configured."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(
                os.path.join(config_dir, "config.json"),
                '{"npm_wrapper": {"scope": "@ziguser"}}',
            )
            monkeypatch.chdir(d)
            mappings = target.shared_template_mappings(make_ctx(d))
            targets = [m["target"] for m in mappings]
            assert "npm-wrapper/package.json" in targets
            assert "npm-wrapper/bin/index.js" in targets
            assert "npm-wrapper/linux-x64/package.json" in targets
            assert "npm-wrapper/win32-x64/package.json" in targets

    def test_mappings_exclude_npm_wrapper_when_not_configured(self, monkeypatch):
        """npm wrapper mappings excluded from shared_template_mappings when not configured."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_BINARY)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            monkeypatch.chdir(d)
            mappings = target.shared_template_mappings(make_ctx(d))
            targets = [m["target"] for m in mappings]
            assert "npm-wrapper/package.json" not in targets

    def test_mappings_exclude_npm_wrapper_for_libraries(self, monkeypatch):
        """npm wrapper mappings excluded for Zig libraries."""
        target = ZigTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.zig.zon"), SAMPLE_ZON)
            _write(os.path.join(d, "build.zig"), BUILD_ZIG_LIBRARY)
            _write(os.path.join(d, "VERSION"), "0.1.0\n")
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(
                os.path.join(config_dir, "config.json"),
                '{"npm_wrapper": {"scope": "@ziguser"}}',
            )
            monkeypatch.chdir(d)
            mappings = target.shared_template_mappings(make_ctx(d))
            targets = [m["target"] for m in mappings]
            assert "npm-wrapper/package.json" not in targets
