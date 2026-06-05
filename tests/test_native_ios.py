"""Tests for NativeIosTarget: detection, version reading/writing, name, and registration."""

import os
import tempfile

from conftest import make_ctx
from rlsbl.targets.native_ios import NativeIosTarget
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets import TARGETS


SAMPLE_PBXPROJ = """\
// !$*UTF8*$!
{
	archiveVersion = 1;
	objectVersion = 56;
	rootObject = 123;
	buildSettings = {
		MARKETING_VERSION = 1.2.3;
		CURRENT_PROJECT_VERSION = 7;
		PRODUCT_BUNDLE_IDENTIFIER = com.example.MyApp;
	};
}
"""

SAMPLE_PBXPROJ_NO_BUILD_NUM = """\
// !$*UTF8*$!
{
	buildSettings = {
		MARKETING_VERSION = 0.5.0;
	};
}
"""

SAMPLE_TUIST = """\
import ProjectDescription

let project = Project(
    name: "MyTuistApp",
    infoPlist: .extendingDefault(with: [
        "CFBundleShortVersionString": "2.0.0",
        "CFBundleVersion": "10",
    ]),
    targets: []
)
"""

SAMPLE_TUIST_NO_BUILD = """\
import ProjectDescription

let project = Project(
    name: "MyTuistApp",
    infoPlist: .extendingDefault(with: [
        "CFBundleShortVersionString": "3.1.0",
    ]),
    targets: []
)
"""


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _make_xcodeproj(dir_path, name="MyApp", pbxproj_content=SAMPLE_PBXPROJ):
    """Create a <name>.xcodeproj/project.pbxproj in dir_path."""
    xcodeproj_dir = os.path.join(dir_path, f"{name}.xcodeproj")
    os.makedirs(xcodeproj_dir, exist_ok=True)
    _write(os.path.join(xcodeproj_dir, "project.pbxproj"), pbxproj_content)
    return xcodeproj_dir


class TestDetect:
    """NativeIosTarget.detect() finds xcodeproj dirs and Tuist projects."""

    def test_detect_ios_app(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d)
            assert target.detect(d) is True

    def test_detect_spm_rejected(self):
        """Package.swift present rejects detection even with xcodeproj."""
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d)
            _write(os.path.join(d, "Package.swift"), "// swift package")
            assert target.detect(d) is False

    def test_detect_tuist(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Project.swift"), SAMPLE_TUIST)
            assert target.detect(d) is True

    def test_detect_no_project(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_xcodeproj_without_version_keys(self):
        """xcodeproj without MARKETING_VERSION is not detected."""
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d, pbxproj_content="// empty project\n")
            assert target.detect(d) is False

    def test_detect_tuist_without_version_key(self):
        """Project.swift without CFBundleShortVersionString is not detected."""
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Project.swift"), "import ProjectDescription\n")
            assert target.detect(d) is False


class TestReadVersion:
    """read_version extracts version from pbxproj and Tuist."""

    def test_read_version_pbxproj(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d)
            assert target.read_version(d) == "1.2.3"

    def test_read_version_tuist(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Project.swift"), SAMPLE_TUIST)
            assert target.read_version(d) == "2.0.0"

    def test_read_version_no_source_raises(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            try:
                target.read_version(d)
                assert False, "Expected ValueError"
            except ValueError:
                pass

    def test_read_version_pbxproj_without_build_num(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d, pbxproj_content=SAMPLE_PBXPROJ_NO_BUILD_NUM)
            assert target.read_version(d) == "0.5.0"


class TestWriteVersion:
    """write_version updates version and increments build number."""

    def test_write_version_pbxproj(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d)
            modified = target.write_version(d, "1.3.0", ctx=make_ctx(d))
            content = _read(os.path.join(d, "MyApp.xcodeproj", "project.pbxproj"))
            assert "MARKETING_VERSION = 1.3.0;" in content
            assert "CURRENT_PROJECT_VERSION = 8;" in content
            assert len(modified) == 1
            assert "project.pbxproj" in modified[0]

    def test_write_version_tuist(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Project.swift"), SAMPLE_TUIST)
            modified = target.write_version(d, "2.1.0", ctx=make_ctx(d))
            content = _read(os.path.join(d, "Project.swift"))
            assert '"CFBundleShortVersionString": "2.1.0"' in content
            assert '"CFBundleVersion": "11"' in content
            assert modified == ["Project.swift"]

    def test_write_version_pbxproj_no_build_num(self):
        """When CURRENT_PROJECT_VERSION is absent, only marketing version is written."""
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d, pbxproj_content=SAMPLE_PBXPROJ_NO_BUILD_NUM)
            target.write_version(d, "0.6.0", ctx=make_ctx(d))
            content = _read(os.path.join(d, "MyApp.xcodeproj", "project.pbxproj"))
            assert "MARKETING_VERSION = 0.6.0;" in content

    def test_write_version_tuist_no_build(self):
        """When CFBundleVersion is absent, only short version is written."""
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Project.swift"), SAMPLE_TUIST_NO_BUILD)
            target.write_version(d, "3.2.0", ctx=make_ctx(d))
            content = _read(os.path.join(d, "Project.swift"))
            assert '"CFBundleShortVersionString": "3.2.0"' in content

    def test_write_version_no_source_raises(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            try:
                target.write_version(d, "1.0.0", ctx=make_ctx(d))
                assert False, "Expected ValueError"
            except ValueError:
                pass


class TestReadName:
    """read_name returns xcodeproj name or directory basename."""

    def test_read_name(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _make_xcodeproj(d, name="CoolApp")
            assert target.read_name(d, ctx=make_ctx(d)) == "CoolApp"

    def test_read_name_fallback_to_dirname(self):
        target = NativeIosTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.read_name(d, ctx=make_ctx(d)) == os.path.basename(d)


class TestProperties:
    """Static properties and protocol conformance."""

    def test_name(self):
        target = NativeIosTarget()
        assert target.name == "native-ios"

    def test_version_file_returns_none(self):
        target = NativeIosTarget()
        assert target.version_file() is None

    def test_is_release_target(self):
        target = NativeIosTarget()
        assert isinstance(target, ReleaseTarget)

    def test_template_dir_returns_none(self):
        target = NativeIosTarget()
        assert target.template_dir() is None

    def test_template_mappings_empty(self):
        target = NativeIosTarget()
        assert target.template_mappings(ctx=make_ctx(".")) == []

    def test_ecosystem(self):
        target = NativeIosTarget()
        assert target.ecosystem == "iOS"

    def test_auto_detectable(self):
        target = NativeIosTarget()
        assert target.auto_detectable == "yes"

    def test_capabilities(self):
        target = NativeIosTarget()
        assert target.capabilities == frozenset({"read_name"})


class TestRegistration:
    """NativeIosTarget is registered in TARGETS."""

    def test_registered_in_targets(self):
        assert "native-ios" in TARGETS
        assert isinstance(TARGETS["native-ios"], NativeIosTarget)
