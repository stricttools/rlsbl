"""Tests for Phase 8: Gradle version catalog, build, lint, and lockfile support."""

import json
import os
import subprocess
import textwrap
from unittest.mock import patch

import pytest

from rlsbl.targets.maven import MavenTarget
from rlsbl.errors import VersionError


# ---------------------------------------------------------------------------
# Phase 8a: Gradle version catalog support
# ---------------------------------------------------------------------------


class TestVersionCatalogRead:
    """read_version from libs.versions.toml with config key."""

    def test_reads_version_from_catalog(self, tmp_project):
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [versions]
            app-version = "1.2.3"
            kotlin = "1.9.0"
        """))
        (tmp_project / "build.gradle.kts").write_text('group = "com.example"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app-version"})
        )

        target = MavenTarget()
        assert target.read_version(str(tmp_project)) == "1.2.3"

    def test_catalog_takes_priority_over_build_gradle(self, tmp_project):
        """Version catalog is priority 0, even if build.gradle.kts has a version."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [versions]
            myver = "3.0.0"
        """))
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "myver"})
        )

        target = MavenTarget()
        assert target.read_version(str(tmp_project)) == "3.0.0"


class TestVersionCatalogWrite:
    """write_version to libs.versions.toml."""

    def test_writes_version_to_catalog(self, tmp_project):
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        catalog = gradle_dir / "libs.versions.toml"
        catalog.write_text(textwrap.dedent("""\
            [versions]
            app-version = "1.0.0"
            kotlin = "1.9.0"
        """))
        (tmp_project / "build.gradle.kts").write_text('group = "com.example"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app-version"})
        )

        target = MavenTarget()
        modified = target.write_version(str(tmp_project), "2.0.0", None)

        assert os.path.join("gradle", "libs.versions.toml") in modified
        assert target.read_version(str(tmp_project)) == "2.0.0"

        # Verify other keys are preserved
        content = catalog.read_text()
        assert "kotlin" in content
        assert '"1.9.0"' in content

    def test_write_preserves_formatting(self, tmp_project):
        """tomlkit preserves comments and formatting."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        catalog = gradle_dir / "libs.versions.toml"
        catalog.write_text(textwrap.dedent("""\
            # Project versions
            [versions]
            app-version = "1.0.0"  # the main version
            kotlin = "1.9.0"
        """))
        (tmp_project / "build.gradle.kts").write_text("")
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app-version"})
        )

        target = MavenTarget()
        target.write_version(str(tmp_project), "2.0.0", None)

        content = catalog.read_text()
        assert "# Project versions" in content
        assert "# the main version" in content


class TestVersionCatalogRichDeclaration:
    """Error on rich version declaration in catalog."""

    def test_rich_version_raises_on_read(self, tmp_project):
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [versions]
            app-version = { strictly = "1.0.0" }
        """))
        (tmp_project / "build.gradle.kts").write_text("")
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app-version"})
        )

        target = MavenTarget()
        with pytest.raises(VersionError, match="Rich version declaration"):
            target.read_version(str(tmp_project))

    def test_rich_version_raises_on_write(self, tmp_project):
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(textwrap.dedent("""\
            [versions]
            app-version = { require = "1.0.0", prefer = "1.0.0" }
        """))
        (tmp_project / "build.gradle.kts").write_text("")
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app-version"})
        )

        target = MavenTarget()
        with pytest.raises(VersionError, match="Rich version declaration"):
            target.write_version(str(tmp_project), "2.0.0", None)


class TestVersionCatalogKeyMissing:
    """Error when version_catalog_key not in config."""

    def test_no_config_file(self, tmp_project):
        """Without config, catalog is skipped and version is read from build.gradle.kts."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text('[versions]\nv = "1.0"\n')
        (tmp_project / "build.gradle.kts").write_text('version = "3.0.0"\n')

        target = MavenTarget()
        assert target.read_version(str(tmp_project)) == "3.0.0"

    def test_config_without_key(self, tmp_project):
        """Config exists but no version_catalog_key -- catalog skipped, falls through."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text('[versions]\nv = "1.0"\n')
        (tmp_project / "build.gradle.kts").write_text('version = "4.0.0"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"publish_mode": "ci"}')

        target = MavenTarget()
        assert target.read_version(str(tmp_project)) == "4.0.0"

    def test_config_with_empty_key(self, tmp_project):
        """Empty version_catalog_key treated as unconfigured -- catalog skipped."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text('[versions]\nv = "1.0"\n')
        (tmp_project / "build.gradle.kts").write_text('version = "5.0.0"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            '{"version_catalog_key": ""}'
        )

        target = MavenTarget()
        assert target.read_version(str(tmp_project)) == "5.0.0"


class TestPhase6eHardErrorRemoved:
    """Phase 6e hard error removed -- catalog now supported."""

    def test_catalog_no_longer_raises_unsupported_error(self, tmp_project):
        """Without version_catalog_key, catalog is silently skipped.

        The old Phase 6e error ("not yet supported") and the Phase 8a
        config-required error are both gone. The catalog is simply
        ignored and the next priority source is used.
        """
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text('[versions]\napp = "1.0"\n')
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        # Falls through to build.gradle.kts -- no error at all
        assert target.read_version(str(tmp_project)) == "1.0.0"

    def test_catalog_works_with_proper_config(self, tmp_project):
        """With proper config, version catalog reads and writes correctly."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text('[versions]\napp = "1.0.0"\n')
        (tmp_project / "build.gradle.kts").write_text('version = "0.0.0"\n')
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"version_catalog_key": "app"})
        )

        target = MavenTarget()
        # Read works
        assert target.read_version(str(tmp_project)) == "1.0.0"
        # Write works
        target.write_version(str(tmp_project), "2.0.0", None)
        assert target.read_version(str(tmp_project)) == "2.0.0"


# ---------------------------------------------------------------------------
# Phase 8b: Build step during release
# ---------------------------------------------------------------------------


class TestBuildGradle:
    """build() runs ./gradlew build for Gradle."""

    def test_runs_gradlew_build(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)

        with patch("rlsbl.targets.maven.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

            target = MavenTarget()
            target.build(str(tmp_project), "1.0.0")

            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["./gradlew", "build"]
            assert mock_run.call_args[1]["cwd"] == str(tmp_project)

    def test_gradlew_build_failure_raises(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)

        with patch("rlsbl.targets.maven.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="BUILD FAILED"
            )

            target = MavenTarget()
            with pytest.raises(RuntimeError, match="gradlew build failed"):
                target.build(str(tmp_project), "1.0.0")


class TestBuildMaven:
    """build() runs mvn package for Maven."""

    def test_runs_mvn_package(self, tmp_project):
        (tmp_project / "pom.xml").write_text("<project><version>1.0.0</version></project>\n")

        with patch("rlsbl.targets.maven.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

            target = MavenTarget()
            target.build(str(tmp_project), "1.0.0")

            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["mvn", "package"]
            assert mock_run.call_args[1]["cwd"] == str(tmp_project)

    def test_mvn_package_failure_raises(self, tmp_project):
        (tmp_project / "pom.xml").write_text("<project><version>1.0.0</version></project>\n")

        with patch("rlsbl.targets.maven.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="BUILD FAILURE"
            )

            target = MavenTarget()
            with pytest.raises(RuntimeError, match="mvn package failed"):
                target.build(str(tmp_project), "1.0.0")

    def test_prefers_gradlew_over_mvn(self, tmp_project):
        """When both gradlew and pom.xml exist, uses gradlew."""
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "pom.xml").write_text("<project><version>1.0.0</version></project>\n")

        with patch("rlsbl.targets.maven.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

            target = MavenTarget()
            target.build(str(tmp_project), "1.0.0")

            assert mock_run.call_args[0][0] == ["./gradlew", "build"]


# ---------------------------------------------------------------------------
# Phase 8c: Lint execution
# ---------------------------------------------------------------------------


class TestLintDetectsDetekt:
    """Lint detects detekt plugin."""

    def test_detekt_in_kts(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "build.gradle.kts").write_text(textwrap.dedent("""\
            plugins {
                id("io.gitlab.arturbosch.detekt") version "1.23.0"
            }
        """))

        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd == ["./gradlew", "detekt"]

    def test_detekt_in_groovy(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "build.gradle").write_text(textwrap.dedent("""\
            plugins {
                id 'io.gitlab.arturbosch.detekt' version '1.23.0'
            }
        """))

        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd == ["./gradlew", "detekt"]


class TestLintDetectsCheckstyle:
    """Lint detects checkstyle plugin."""

    def test_checkstyle_in_kts(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "build.gradle.kts").write_text(textwrap.dedent("""\
            plugins {
                checkstyle
            }
        """))

        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd == ["./gradlew", "checkstyleMain"]


class TestLintFallback:
    """Lint falls back to ./gradlew check."""

    def test_fallback_when_no_lint_plugin(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "build.gradle.kts").write_text(textwrap.dedent("""\
            plugins {
                id("java-library")
            }
        """))

        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd == ["./gradlew", "check"]

    def test_no_gradlew_returns_none(self, tmp_project):
        """Without gradlew, lint command is None."""
        (tmp_project / "build.gradle.kts").write_text("")
        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd is None

    def test_fallback_with_no_build_file(self, tmp_project):
        """With gradlew but no build file, falls back to check."""
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)

        cmd = MavenTarget.detect_lint_command(str(tmp_project))
        assert cmd == ["./gradlew", "check"]


# ---------------------------------------------------------------------------
# Phase 8d: Lockfile sync
# ---------------------------------------------------------------------------


class TestGradleLockfileSpec:
    """gradle.lockfile in _LOCKFILE_SPECS."""

    def test_gradle_lockfile_in_specs(self):
        from rlsbl.commands.release.execute import _LOCKFILE_SPECS

        gradle_specs = [s for s in _LOCKFILE_SPECS if s[0] == "gradle.lockfile"]
        assert len(gradle_specs) == 1

        lockfile, tool_name, sync_cmd, guard_file = gradle_specs[0]
        assert lockfile == "gradle.lockfile"
        assert tool_name == "gradle"
        assert sync_cmd == ["./gradlew", "dependencies", "--write-locks"]
        assert guard_file is None
