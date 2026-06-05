"""Tests for NativeAndroidTarget: detection, version read/write, name reading, and maven mutual exclusion."""

import os
import tempfile

from conftest import make_ctx
from rlsbl.targets.native_android import NativeAndroidTarget
from rlsbl.targets.maven import MavenTarget
from rlsbl.targets import TARGETS


SAMPLE_ANDROID_APP_KTS = """\
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.myapp"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.example.myapp"
        minSdk = 24
        targetSdk = 34
        versionCode 42
        versionName "1.5.0"
    }
}
"""

SAMPLE_ANDROID_APP_GROOVY = """\
apply plugin: 'com.android.application'

android {
    compileSdkVersion 34

    defaultConfig {
        applicationId "com.example.groovyapp"
        minSdkVersion 21
        targetSdkVersion 34
        versionCode 10
        versionName "2.0.0"
    }
}
"""

SAMPLE_ANDROID_LIBRARY = """\
plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.mylib"
    compileSdk = 34

    defaultConfig {
        minSdk = 24
        targetSdk = 34
    }
}
"""

SAMPLE_PURE_JAVA = """\
plugins {
    id("java")
    id("application")
}

group = "com.example"
version = "1.0.0"

application {
    mainClass.set("com.example.Main")
}
"""


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestNativeAndroidDetect:
    """NativeAndroidTarget.detect() checks for com.android.application in build.gradle."""

    def test_detect_android_app(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            assert target.detect(d) is True

    def test_detect_android_app_groovy(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle"), SAMPLE_ANDROID_APP_GROOVY)
            assert target.detect(d) is True

    def test_detect_android_library(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_LIBRARY)
            assert target.detect(d) is False

    def test_detect_pure_java(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_PURE_JAVA)
            assert target.detect(d) is False

    def test_detect_no_file(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            assert target.detect(d) is False


class TestNativeAndroidReadVersion:
    """read_version extracts versionName from build.gradle."""

    def test_read_version(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            assert target.read_version(d) == "1.5.0"

    def test_read_version_groovy(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle"), SAMPLE_ANDROID_APP_GROOVY)
            assert target.read_version(d) == "2.0.0"

    def test_read_version_raises_when_missing(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_PURE_JAVA)
            try:
                target.read_version(d)
                assert False, "Expected ValueError"
            except ValueError:
                pass


class TestNativeAndroidWriteVersion:
    """write_version updates versionName and increments versionCode."""

    def test_write_version(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            modified = target.write_version(d, "2.0.0", make_ctx(d))
            assert modified == ["build.gradle.kts"]
            assert target.read_version(d) == "2.0.0"
            # versionCode should be incremented from 42 to 43
            content = _read(os.path.join(d, "build.gradle.kts"))
            assert "versionCode 43" in content

    def test_write_version_groovy(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle"), SAMPLE_ANDROID_APP_GROOVY)
            modified = target.write_version(d, "3.0.0", make_ctx(d))
            assert modified == ["build.gradle"]
            assert target.read_version(d) == "3.0.0"
            content = _read(os.path.join(d, "build.gradle"))
            assert "versionCode 11" in content

    def test_write_version_no_tmp_left_behind(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            target.write_version(d, "2.0.0", make_ctx(d))
            files = os.listdir(d)
            assert "build.gradle.kts.tmp" not in files

    def test_write_version_preserves_other_content(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            target.write_version(d, "2.0.0", make_ctx(d))
            content = _read(os.path.join(d, "build.gradle.kts"))
            assert 'applicationId = "com.example.myapp"' in content
            assert 'namespace = "com.example.myapp"' in content


class TestNativeAndroidReadName:
    """read_name extracts applicationId from build.gradle."""

    def test_read_name(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            assert target.read_name(d, ctx=make_ctx(d)) == "com.example.myapp"

    def test_read_name_fallback(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            # Write a file with no applicationId
            content = SAMPLE_ANDROID_APP_KTS.replace(
                '        applicationId = "com.example.myapp"\n', ""
            )
            _write(os.path.join(d, "build.gradle.kts"), content)
            result = target.read_name(d, ctx=make_ctx(d))
            assert result == os.path.basename(d)


class TestNativeAndroidProperties:
    """Static properties and registration."""

    def test_name(self):
        target = NativeAndroidTarget()
        assert target.name == "native-android"

    def test_ecosystem(self):
        target = NativeAndroidTarget()
        assert target.ecosystem == "Android"

    def test_capabilities(self):
        target = NativeAndroidTarget()
        assert target.capabilities == frozenset({"read_name"})

    def test_detection_files_empty(self):
        target = NativeAndroidTarget()
        assert target.detection_files == ()

    def test_version_file_default(self):
        target = NativeAndroidTarget()
        assert target.version_file() == "build.gradle"

    def test_version_file_with_kts(self):
        target = NativeAndroidTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), "")
            assert target.version_file(d) == "build.gradle.kts"

    def test_template_dir_none(self):
        target = NativeAndroidTarget()
        assert target.template_dir() is None

    def test_template_mappings_empty(self):
        target = NativeAndroidTarget()
        assert target.template_mappings(ctx=None) == []

    def test_registered_in_targets(self):
        assert "native-android" in TARGETS
        assert isinstance(TARGETS["native-android"], NativeAndroidTarget)


class TestMavenMutualExclusion:
    """Maven target returns False for Android application projects."""

    def test_maven_excludes_android_app(self):
        maven = MavenTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_APP_KTS)
            assert maven.detect(d) is False

    def test_maven_includes_android_library(self):
        maven = MavenTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_ANDROID_LIBRARY)
            assert maven.detect(d) is True

    def test_maven_includes_pure_java(self):
        maven = MavenTarget()
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.gradle.kts"), SAMPLE_PURE_JAVA)
            assert maven.detect(d) is True

    def test_maven_includes_pom_xml(self):
        maven = MavenTarget()
        with tempfile.TemporaryDirectory() as d:
            pom = '<?xml version="1.0"?>\n<project><version>1.0</version></project>\n'
            _write(os.path.join(d, "pom.xml"), pom)
            assert maven.detect(d) is True
