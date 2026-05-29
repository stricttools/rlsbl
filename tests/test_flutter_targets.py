"""Tests for Flutter iOS and Android targets, release file mode validation, and native change detection."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.targets.flutter_ios import FlutterIosTarget
from rlsbl.targets.flutter_android import FlutterAndroidTarget
from rlsbl.targets.dart import DartTarget
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets import TARGETS, detect_targets
from rlsbl.release_file import read_release_file
from rlsbl.targets.native_changes import detect_native_changes


SAMPLE_FLUTTER_PUBSPEC = """\
name: my_flutter_app
description: A Flutter application.
version: 1.0.0+1

environment:
  sdk: ^3.0.0

flutter:
  uses-material-design: true
"""

SAMPLE_DART_PUBSPEC = """\
name: my_dart_lib
description: A Dart library.
version: 2.0.0

environment:
  sdk: ^3.0.0

dependencies:
  http: ^0.13.0
"""

SAMPLE_FLUTTER_PUBSPEC_NO_VERSION = """\
name: my_flutter_app

flutter:
  uses-material-design: true
"""


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# -- Detection tests --

class TestFlutterIosDetect:
    """FlutterIosTarget.detect() returns True only for Flutter projects."""

    def test_detect_flutter_project(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            assert target.detect(d) is True

    def test_detect_false_for_dart_project(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_DART_PUBSPEC)
            assert target.detect(d) is False

    def test_detect_false_without_pubspec(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_false_with_empty_file(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), "")
            assert target.detect(d) is False


class TestFlutterAndroidDetect:
    """FlutterAndroidTarget.detect() returns True only for Flutter projects."""

    def test_detect_flutter_project(self):
        target = FlutterAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            assert target.detect(d) is True

    def test_detect_false_for_dart_project(self):
        target = FlutterAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_DART_PUBSPEC)
            assert target.detect(d) is False

    def test_detect_false_without_pubspec(self):
        target = FlutterAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False


class TestDartExcludesFlutter:
    """DartTarget.detect() returns False when flutter: section is present."""

    def test_dart_detect_false_for_flutter(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            assert target.detect(d) is False

    def test_dart_detect_true_for_dart(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_DART_PUBSPEC)
            assert target.detect(d) is True


# -- Auto-detection tests --

class TestAutoDetection:
    """Auto-detection discovers both flutter-ios and flutter-android for Flutter projects."""

    def test_flutter_project_detects_both_platforms(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            found = detect_targets(d)
            names = sorted(t.name for t in found)
            assert "flutter-ios" in names
            assert "flutter-android" in names
            assert "dart" not in names

    def test_dart_project_detects_only_dart(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_DART_PUBSPEC)
            found = detect_targets(d)
            names = [t.name for t in found]
            assert "dart" in names
            assert "flutter-ios" not in names
            assert "flutter-android" not in names


# -- Tag format tests --

class TestFlutterIosTagFormats:
    """FlutterIosTarget uses {name}-ios@v{version} tag format."""

    def test_monorepo_tag_format(self):
        target = FlutterIosTarget()
        assert target.monorepo_tag_format("myapp", "1.2.3") == "myapp-ios@v1.2.3"

    def test_monorepo_tag_glob(self):
        target = FlutterIosTarget()
        assert target.monorepo_tag_glob("myapp") == "myapp-ios@v*"

    def test_tag_format_inherited(self):
        """tag_format is inherited from BaseTarget (plain v{version})."""
        target = FlutterIosTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"


class TestFlutterAndroidTagFormats:
    """FlutterAndroidTarget uses {name}-android@v{version} tag format."""

    def test_monorepo_tag_format(self):
        target = FlutterAndroidTarget()
        assert target.monorepo_tag_format("myapp", "1.2.3") == "myapp-android@v1.2.3"

    def test_monorepo_tag_glob(self):
        target = FlutterAndroidTarget()
        assert target.monorepo_tag_glob("myapp") == "myapp-android@v*"

    def test_tag_format_inherited(self):
        target = FlutterAndroidTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"


# -- Version read/write inheritance tests --

class TestFlutterVersionInheritance:
    """Flutter targets inherit version read/write from DartTarget."""

    def test_ios_read_version(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            assert target.read_version(d) == "1.0.0"

    def test_android_read_version(self):
        target = FlutterAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            assert target.read_version(d) == "1.0.0"

    def test_ios_write_version(self):
        target = FlutterIosTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            modified = target.write_version(d, "2.0.0", d)
            assert modified == ["pubspec.yaml"]
            assert target.read_version(d) == "2.0.0"

    def test_android_write_version(self):
        target = FlutterAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_FLUTTER_PUBSPEC)
            modified = target.write_version(d, "2.0.0", d)
            assert modified == ["pubspec.yaml"]
            assert target.read_version(d) == "2.0.0"

    def test_version_file(self):
        assert FlutterIosTarget().version_file() == "pubspec.yaml"
        assert FlutterAndroidTarget().version_file() == "pubspec.yaml"


# -- Properties tests --

class TestFlutterTargetProperties:
    """Static properties of Flutter targets."""

    def test_ios_name(self):
        assert FlutterIosTarget().name == "flutter-ios"

    def test_android_name(self):
        assert FlutterAndroidTarget().name == "flutter-android"

    def test_ios_is_release_target(self):
        assert isinstance(FlutterIosTarget(), ReleaseTarget)

    def test_android_is_release_target(self):
        assert isinstance(FlutterAndroidTarget(), ReleaseTarget)


# -- Registration tests --

class TestFlutterTargetRegistration:
    """Flutter targets are registered in TARGETS dict."""

    def test_ios_registered(self):
        assert "flutter-ios" in TARGETS
        assert isinstance(TARGETS["flutter-ios"], FlutterIosTarget)

    def test_android_registered(self):
        assert "flutter-android" in TARGETS
        assert isinstance(TARGETS["flutter-android"], FlutterAndroidTarget)


# -- Release file mode validation tests --

class TestReleaseFileFlutterMode:
    """Flutter targets require mode in release file."""

    def test_flutter_target_requires_mode(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter-ios"]\n'
            'exclude = []\n'
        )
        with pytest.raises(ValueError, match="requires.*mode"):
            read_release_file(str(f))

    def test_flutter_target_with_mode_ota(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter-ios"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter-ios]\n"
            'mode = "ota"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.targets["flutter-ios"]["mode"] == "ota"

    def test_flutter_target_with_mode_build(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "minor"\n'
            'include = ["flutter-android"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter-android]\n"
            'mode = "build"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.targets["flutter-android"]["mode"] == "build"

    def test_both_flutter_targets_same_mode_ok(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter-ios", "flutter-android"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter-ios]\n"
            'mode = "ota"\n'
            "\n"
            "[targets.flutter-android]\n"
            'mode = "ota"\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.targets["flutter-ios"]["mode"] == "ota"
        assert cfg.targets["flutter-android"]["mode"] == "ota"

    def test_flutter_targets_different_modes_error(self, tmp_path):
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["flutter-ios", "flutter-android"]\n'
            'exclude = []\n'
            "\n"
            "[targets.flutter-ios]\n"
            'mode = "ota"\n'
            "\n"
            "[targets.flutter-android]\n"
            'mode = "build"\n'
        )
        with pytest.raises(ValueError, match="same mode"):
            read_release_file(str(f))

    def test_non_flutter_target_no_mode_required(self, tmp_path):
        """Non-flutter targets do not require a mode field."""
        f = tmp_path / "release.toml"
        f.write_text(
            'bump = "patch"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
        )
        cfg = read_release_file(str(f))
        assert cfg.targets == {}


# -- Native file detection tests --

class TestNativeFileDetection:
    """detect_native_changes filters git diff output for platform directories."""

    def test_detects_ios_changes(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ios/Runner/AppDelegate.swift\nlib/main.dart\n"
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert result == ["ios/Runner/AppDelegate.swift"]

    def test_detects_android_changes(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "android/app/build.gradle\nlib/main.dart\n"
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert result == ["android/app/build.gradle"]

    def test_no_native_changes(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "lib/main.dart\nlib/utils.dart\npubspec.yaml\n"
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert result == []

    def test_multiple_native_directories(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "ios/Runner/Info.plist\n"
            "android/app/build.gradle\n"
            "macos/Runner/AppDelegate.swift\n"
            "web/index.html\n"
            "lib/main.dart\n"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert len(result) == 4
        assert "ios/Runner/Info.plist" in result
        assert "android/app/build.gradle" in result
        assert "macos/Runner/AppDelegate.swift" in result
        assert "web/index.html" in result

    def test_git_failure_returns_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert result == []

    def test_empty_diff_returns_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = detect_native_changes(".", "v1.0.0")
        assert result == []


# -- Changelog release_type tests --

class TestChangelogReleaseType:
    """release_type field in changelog entries."""

    def test_parse_entry_with_release_type(self):
        from rlsbl.changelog.schema import parse_entry
        entry = parse_entry(
            '{"commits":["abc123"],"user_facing":true,'
            '"description":"OTA patch","type":"fix","release_type":"ota"}'
        )
        assert entry.release_type == "ota"

    def test_parse_entry_without_release_type(self):
        from rlsbl.changelog.schema import parse_entry
        entry = parse_entry(
            '{"commits":["abc123"],"user_facing":false}'
        )
        assert entry.release_type is None

    def test_serialize_entry_with_release_type(self):
        from rlsbl.changelog.schema import ChangelogEntry, serialize_entry
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Build release",
            type="feature",
            release_type="build",
        )
        serialized = serialize_entry(entry)
        assert '"release_type":"build"' in serialized

    def test_serialize_entry_without_release_type(self):
        from rlsbl.changelog.schema import ChangelogEntry, serialize_entry
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Regular fix",
            type="fix",
        )
        serialized = serialize_entry(entry)
        assert "release_type" not in serialized

    def test_validate_schema_valid_release_type(self):
        from rlsbl.changelog.schema import ChangelogEntry, validate_schema
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="OTA fix",
            type="fix",
            release_type="ota",
        )
        assert validate_schema(entry) == []

    def test_validate_schema_invalid_release_type(self):
        from rlsbl.changelog.schema import ChangelogEntry, validate_schema
        entry = ChangelogEntry(
            commits=["abc123"],
            user_facing=True,
            description="Bad type",
            type="fix",
            release_type="deploy",
        )
        errors = validate_schema(entry)
        assert any("release_type" in e for e in errors)

    def test_generate_version_section_ota_marker(self):
        from rlsbl.changelog.schema import ChangelogEntry
        from rlsbl.changelog.generate import generate_version_section
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="OTA fix",
                type="fix",
                release_type="ota",
            ),
        ]
        section = generate_version_section("1.2.3", entries)
        assert "## 1.2.3 (OTA)" in section

    def test_generate_version_section_build_marker(self):
        from rlsbl.changelog.schema import ChangelogEntry
        from rlsbl.changelog.generate import generate_version_section
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="New feature",
                type="feature",
                release_type="build",
            ),
        ]
        section = generate_version_section("2.0.0", entries)
        assert "## 2.0.0 (BUILD)" in section

    def test_generate_version_section_no_marker_when_no_release_type(self):
        from rlsbl.changelog.schema import ChangelogEntry
        from rlsbl.changelog.generate import generate_version_section
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="Normal fix",
                type="fix",
            ),
        ]
        section = generate_version_section("1.0.0", entries)
        assert "## 1.0.0\n" in section
        assert "(OTA)" not in section
        assert "(BUILD)" not in section

    def test_generate_version_section_no_marker_mixed_types(self):
        """When entries have different release_types, no marker is added."""
        from rlsbl.changelog.schema import ChangelogEntry
        from rlsbl.changelog.generate import generate_version_section
        entries = [
            ChangelogEntry(
                commits=["abc"],
                user_facing=True,
                description="OTA fix",
                type="fix",
                release_type="ota",
            ),
            ChangelogEntry(
                commits=["def"],
                user_facing=True,
                description="Build feature",
                type="feature",
                release_type="build",
            ),
        ]
        section = generate_version_section("1.0.0", entries)
        assert "## 1.0.0\n" in section
