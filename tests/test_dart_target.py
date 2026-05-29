"""Tests for DartTarget: detection, version read/write, build numbers, and registration."""

import json
import os
import tempfile

from rlsbl.targets.dart import DartTarget
from rlsbl.targets.protocol import ReleaseTarget
from rlsbl.targets import TARGETS


SAMPLE_PUBSPEC = """\
name: my_dart_app
description: A sample Dart application.
version: 1.2.3
license: MIT

environment:
  sdk: ^3.0.0

dependencies:
  http: ^0.13.0
"""

SAMPLE_PUBSPEC_WITH_BUILD = """\
name: my_dart_app
description: A sample Dart application.
version: 1.2.3+4

environment:
  sdk: ^3.0.0
"""

SAMPLE_PUBSPEC_FLUTTER = """\
name: my_flutter_app
description: A Flutter application.
version: 1.0.0+1

environment:
  sdk: ^3.0.0

flutter:
  uses-material-design: true
"""

SAMPLE_PUBSPEC_WITH_COMMENT = """\
name: my_dart_app
# This is the app version
version: 2.0.0

environment:
  sdk: ^3.0.0
"""

SAMPLE_PUBSPEC_NO_VERSION = """\
name: my_dart_app
description: Missing version field.
"""


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestDartTargetDetect:
    """DartTarget.detect() checks for pubspec.yaml without a flutter: section."""

    def test_detect_with_pubspec(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            assert target.detect(d) is True

    def test_detect_false_without_pubspec(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False

    def test_detect_false_with_flutter(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_FLUTTER)
            assert target.detect(d) is False

    def test_detect_false_with_empty_file(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), "")
            assert target.detect(d) is False


class TestDartTargetReadVersion:
    """read_version extracts semver, stripping build number."""

    def test_read_simple_version(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            assert target.read_version(d) == "1.2.3"

    def test_read_version_strips_build_number(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_WITH_BUILD)
            assert target.read_version(d) == "1.2.3"

    def test_read_version_raises_when_missing(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_NO_VERSION)
            try:
                target.read_version(d)
                assert False, "Expected ValueError"
            except ValueError:
                pass


class TestDartTargetWriteVersion:
    """write_version round-trips correctly, preserving comments."""

    def test_write_version_simple(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            modified = target.write_version(d, "2.0.0", d)
            assert modified == ["pubspec.yaml"]
            assert target.read_version(d) == "2.0.0"

    def test_write_version_preserves_other_fields(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            target.write_version(d, "3.0.0", d)
            content = _read(os.path.join(d, "pubspec.yaml"))
            assert "name: my_dart_app" in content
            assert "http:" in content

    def test_write_version_preserves_comments(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_WITH_COMMENT)
            target.write_version(d, "3.0.0", d)
            content = _read(os.path.join(d, "pubspec.yaml"))
            assert "# This is the app version" in content
            assert "3.0.0" in content

    def test_write_version_preserves_build_number(self):
        """When build_number is not enabled, preserve existing +N."""
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_WITH_BUILD)
            target.write_version(d, "2.0.0", d)
            from ruamel.yaml import YAML
            yaml = YAML(typ="safe")
            with open(os.path.join(d, "pubspec.yaml"), "r") as f:
                data = yaml.load(f)
            assert data["version"] == "2.0.0+4"

    def test_write_version_no_build_number_when_absent(self):
        """When original has no +N and build_number not enabled, write plain version."""
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            target.write_version(d, "2.0.0", d)
            from ruamel.yaml import YAML
            yaml = YAML(typ="safe")
            with open(os.path.join(d, "pubspec.yaml"), "r") as f:
                data = yaml.load(f)
            assert data["version"] == "2.0.0"

    def test_write_version_no_tmp_left_behind(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            target.write_version(d, "2.0.0", d)
            files = os.listdir(d)
            assert "pubspec.yaml.tmp" not in files


class TestDartTargetWriteVersionBuildNumber:
    """write_version with build_number config enabled."""

    def test_build_number_increment(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_WITH_BUILD)
            # Create .rlsbl/config.json with build_number enabled
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(os.path.join(config_dir, "config.json"), json.dumps({
                "build_number": {"enabled": True, "strategy": "increment"}
            }))
            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                target.write_version(d, "2.0.0", d)
            finally:
                os.chdir(old_cwd)
            from ruamel.yaml import YAML
            yaml = YAML(typ="safe")
            with open(os.path.join(d, "pubspec.yaml"), "r") as f:
                data = yaml.load(f)
            # Old was +4, so new should be +5
            assert data["version"] == "2.0.0+5"

    def test_build_number_increment_from_zero(self):
        """When build_number enabled but no existing +N, start from 0 -> 1."""
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            config_dir = os.path.join(d, ".rlsbl")
            os.makedirs(config_dir)
            _write(os.path.join(config_dir, "config.json"), json.dumps({
                "build_number": {"enabled": True, "strategy": "increment"}
            }))
            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                target.write_version(d, "2.0.0", d)
            finally:
                os.chdir(old_cwd)
            from ruamel.yaml import YAML
            yaml = YAML(typ="safe")
            with open(os.path.join(d, "pubspec.yaml"), "r") as f:
                data = yaml.load(f)
            assert data["version"] == "2.0.0+1"


class TestDartTargetReadName:
    """read_name extracts name from pubspec.yaml."""

    def test_read_name(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            assert target.read_name(d) == "my_dart_app"

    def test_read_name_none_without_pubspec(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.read_name(d) is None


class TestDartTargetReadMetadata:
    """read_metadata extracts description and license."""

    def test_read_metadata_full(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            meta = target.read_metadata(d)
            assert meta["description"] == "A sample Dart application."
            assert meta["license"] == "MIT"

    def test_read_metadata_empty_without_pubspec(self):
        target = DartTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.read_metadata(d) == {}


class TestDartTargetProperties:
    """Static properties and method return values."""

    def test_name(self):
        target = DartTarget()
        assert target.name == "dart"

    def test_version_file(self):
        target = DartTarget()
        assert target.version_file() == "pubspec.yaml"

    def test_is_release_target(self):
        target = DartTarget()
        assert isinstance(target, ReleaseTarget)

    def test_tag_format_inherited(self):
        target = DartTarget()
        assert target.tag_format("1.2.3") == "v1.2.3"


class TestDartTargetRegistered:
    """DartTarget is registered in TARGETS."""

    def test_registered_in_targets(self):
        assert "dart" in TARGETS
        assert isinstance(TARGETS["dart"], DartTarget)

    def test_auto_detects_pubspec(self):
        """Auto-detection finds dart target when pubspec.yaml is present."""
        from rlsbl.targets import detect_targets
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC)
            found = detect_targets(d)
            names = [t.name for t in found]
            assert "dart" in names

    def test_auto_detect_excludes_flutter(self):
        """Auto-detection skips dart target for Flutter projects."""
        from rlsbl.targets import detect_targets
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pubspec.yaml"), SAMPLE_PUBSPEC_FLUTTER)
            found = detect_targets(d)
            names = [t.name for t in found]
            assert "dart" not in names
