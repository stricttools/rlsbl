"""Tests for Maven Central validation (Phase 6c), maven test execution (Phase 6d), and Gradle version catalog detection (Phase 6e)."""

import os
import subprocess
import textwrap
from unittest.mock import patch

import pytest

from rlsbl.maven_central import validate_maven_central_metadata, _validate_pom_metadata, _check_source_javadoc_jars
from rlsbl.testing import run_project_tests
from rlsbl.targets.maven import MavenTarget
from rlsbl.errors import VersionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPLETE_POM = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <project xmlns="http://maven.apache.org/POM/4.0.0">
        <modelVersion>4.0.0</modelVersion>
        <groupId>com.example</groupId>
        <artifactId>mylib</artifactId>
        <version>1.0.0</version>
        <name>My Library</name>
        <description>A test library</description>
        <url>https://github.com/example/mylib</url>
        <licenses>
            <license>
                <name>MIT License</name>
                <url>https://opensource.org/licenses/MIT</url>
            </license>
        </licenses>
        <developers>
            <developer>
                <name>Test Developer</name>
            </developer>
        </developers>
        <scm>
            <connection>scm:git:git://github.com/example/mylib.git</connection>
            <developerConnection>scm:git:ssh://github.com/example/mylib.git</developerConnection>
            <url>https://github.com/example/mylib</url>
        </scm>
    </project>
""")

# POM without namespace (also valid)
COMPLETE_POM_NO_NS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <project>
        <modelVersion>4.0.0</modelVersion>
        <groupId>com.example</groupId>
        <artifactId>mylib</artifactId>
        <version>1.0.0</version>
        <name>My Library</name>
        <description>A test library</description>
        <url>https://github.com/example/mylib</url>
        <licenses>
            <license>
                <name>MIT License</name>
                <url>https://opensource.org/licenses/MIT</url>
            </license>
        </licenses>
        <developers>
            <developer>
                <name>Test Developer</name>
            </developer>
        </developers>
        <scm>
            <connection>scm:git:git://github.com/example/mylib.git</connection>
            <developerConnection>scm:git:ssh://github.com/example/mylib.git</developerConnection>
            <url>https://github.com/example/mylib</url>
        </scm>
    </project>
""")

GRADLE_BUILD_WITH_JARS = textwrap.dedent("""\
    plugins {
        id("java-library")
    }

    java {
        withSourcesJar()
        withJavadocJar()
    }

    group = "com.example"
    version = "1.0.0"
""")

GRADLE_BUILD_VANNIKTECH = textwrap.dedent("""\
    plugins {
        id("com.vanniktech.maven.publish") version "0.25.3"
    }

    group = "com.example"
    version = "1.0.0"
""")


# ---------------------------------------------------------------------------
# Phase 6c: POM metadata validation
# ---------------------------------------------------------------------------

class TestPomMetadataComplete:
    """POM metadata check passes with complete POM."""

    def test_complete_pom_with_namespace(self, tmp_project):
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(COMPLETE_POM)
        errors = _validate_pom_metadata(str(pom_path))
        assert errors == []

    def test_complete_pom_without_namespace(self, tmp_project):
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(COMPLETE_POM_NO_NS)
        errors = _validate_pom_metadata(str(pom_path))
        assert errors == []


class TestPomMetadataMissingElements:
    """POM metadata check fails with missing elements (each one)."""

    def test_missing_name(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace("<name>My Library</name>", "")
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<name>" in e for e in errors)

    def test_missing_description(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace("<description>A test library</description>", "")
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<description>" in e for e in errors)

    def test_missing_url(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            "    <url>https://github.com/example/mylib</url>\n    <licenses>",
            "    <licenses>"
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<url>" in e for e in errors)

    def test_missing_licenses(self, tmp_project):
        # Remove entire licenses block
        pom = COMPLETE_POM_NO_NS.replace(
            textwrap.dedent("""\
                <licenses>
                        <license>
                            <name>MIT License</name>
                            <url>https://opensource.org/licenses/MIT</url>
                        </license>
                    </licenses>"""),
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<licenses>" in e for e in errors)

    def test_license_missing_name(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace("<name>MIT License</name>", "")
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("license" in e.lower() and "name" in e.lower() for e in errors)

    def test_license_missing_url(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            "<url>https://opensource.org/licenses/MIT</url>",
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("license" in e.lower() and "url" in e.lower() for e in errors)

    def test_missing_developers(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            textwrap.dedent("""\
                <developers>
                        <developer>
                            <name>Test Developer</name>
                        </developer>
                    </developers>"""),
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<developers>" in e for e in errors)

    def test_developer_missing_name(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace("<name>Test Developer</name>", "")
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("developer" in e.lower() and "name" in e.lower() for e in errors)

    def test_missing_scm(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            textwrap.dedent("""\
                <scm>
                        <connection>scm:git:git://github.com/example/mylib.git</connection>
                        <developerConnection>scm:git:ssh://github.com/example/mylib.git</developerConnection>
                        <url>https://github.com/example/mylib</url>
                    </scm>"""),
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<scm>" in e for e in errors)

    def test_scm_missing_connection(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            "<connection>scm:git:git://github.com/example/mylib.git</connection>",
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<connection>" in e for e in errors)

    def test_scm_missing_developer_connection(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace(
            "<developerConnection>scm:git:ssh://github.com/example/mylib.git</developerConnection>",
            ""
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<developerConnection>" in e for e in errors)

    def test_scm_missing_url(self, tmp_project):
        # The SCM url is the last <url> in the <scm> block
        pom = COMPLETE_POM_NO_NS.replace(
            "        <url>https://github.com/example/mylib</url>\n    </scm>",
            "    </scm>"
        )
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("scm" in e.lower() and "url" in e.lower() for e in errors)

    def test_empty_name_element(self, tmp_project):
        pom = COMPLETE_POM_NO_NS.replace("<name>My Library</name>", "<name></name>")
        pom_path = tmp_project / "pom.xml"
        pom_path.write_text(pom)
        errors = _validate_pom_metadata(str(pom_path))
        assert any("<name>" in e for e in errors)


class TestSourceJavadocJars:
    """Check sources/javadoc jar detection in build files."""

    def test_gradle_with_sources_and_javadoc(self, tmp_project):
        (tmp_project / "build.gradle.kts").write_text(GRADLE_BUILD_WITH_JARS)
        errors = _check_source_javadoc_jars(str(tmp_project))
        assert errors == []

    def test_vanniktech_plugin(self, tmp_project):
        (tmp_project / "build.gradle.kts").write_text(GRADLE_BUILD_VANNIKTECH)
        errors = _check_source_javadoc_jars(str(tmp_project))
        assert errors == []

    def test_missing_sources_jar(self, tmp_project):
        content = textwrap.dedent("""\
            plugins { id("java-library") }
            java { withJavadocJar() }
            version = "1.0.0"
        """)
        (tmp_project / "build.gradle.kts").write_text(content)
        errors = _check_source_javadoc_jars(str(tmp_project))
        assert any("sources" in e.lower() for e in errors)
        # Javadoc should be fine
        assert not any("javadoc" in e.lower() for e in errors)

    def test_missing_javadoc_jar(self, tmp_project):
        content = textwrap.dedent("""\
            plugins { id("java-library") }
            java { withSourcesJar() }
            version = "1.0.0"
        """)
        (tmp_project / "build.gradle.kts").write_text(content)
        errors = _check_source_javadoc_jars(str(tmp_project))
        assert any("javadoc" in e.lower() for e in errors)
        # Sources should be fine
        assert not any("sources" in e.lower() for e in errors)

    def test_maven_plugins(self, tmp_project):
        pom = textwrap.dedent("""\
            <project>
                <build>
                    <plugins>
                        <plugin>
                            <artifactId>maven-source-plugin</artifactId>
                        </plugin>
                        <plugin>
                            <artifactId>maven-javadoc-plugin</artifactId>
                        </plugin>
                    </plugins>
                </build>
            </project>
        """)
        (tmp_project / "pom.xml").write_text(pom)
        errors = _check_source_javadoc_jars(str(tmp_project))
        assert errors == []


class TestValidateMavenCentralMetadata:
    """Integration test for the full validate_maven_central_metadata function."""

    def test_complete_project_passes(self, tmp_project):
        """Complete Maven project passes all checks."""
        (tmp_project / "pom.xml").write_text(COMPLETE_POM_NO_NS)
        (tmp_project / "build.gradle.kts").write_text(GRADLE_BUILD_WITH_JARS)
        errors = validate_maven_central_metadata(str(tmp_project))
        assert errors == []

    def test_no_pom_no_gradlew(self, tmp_project):
        """Project with only build.gradle but no pom.xml and no gradlew fails gracefully."""
        (tmp_project / "build.gradle.kts").write_text(GRADLE_BUILD_WITH_JARS)
        errors = validate_maven_central_metadata(str(tmp_project))
        assert any("no pom" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Phase 6d: Maven test execution
# ---------------------------------------------------------------------------

class TestMavenTestExecutionGradle:
    """Maven test execution invokes ./gradlew test for Gradle projects."""

    def test_gradle_test(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["./gradlew", "test"]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_gradle_test_failure(self, tmp_project):
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is False


class TestMavenTestExecutionMaven:
    """Maven test execution invokes mvn test for Maven-only projects."""

    def test_mvn_test(self, tmp_project):
        (tmp_project / "pom.xml").write_text(COMPLETE_POM_NO_NS)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0] == ["mvn", "test"]
            assert mock_run.call_args.kwargs.get("cwd") == str(tmp_project)

    def test_mvn_test_failure(self, tmp_project):
        (tmp_project / "pom.xml").write_text(COMPLETE_POM_NO_NS)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is False

    def test_prefers_gradlew_over_pom(self, tmp_project):
        """When both gradlew and pom.xml exist, prefers gradlew."""
        (tmp_project / "gradlew").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_project / "gradlew"), 0o755)
        (tmp_project / "pom.xml").write_text(COMPLETE_POM_NO_NS)

        with patch("rlsbl.testing.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is True
            assert mock_run.call_args[0][0] == ["./gradlew", "test"]

    def test_no_gradlew_no_pom_skips(self, tmp_project):
        """When neither gradlew nor pom.xml exist, skips tests."""
        with patch("rlsbl.testing.subprocess.run") as mock_run:
            result = run_project_tests("maven", project_dir=str(tmp_project))

            assert result is True
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 6e: Version catalog detection
# ---------------------------------------------------------------------------

class TestVersionCatalogDetection:
    """Gradle version catalog detection raises hard error."""

    def test_version_catalog_raises_error(self, tmp_project):
        """Presence of gradle/libs.versions.toml raises VersionError."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text("[versions]\n")
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        with pytest.raises(VersionError, match="version catalog"):
            target.read_version(str(tmp_project))

    def test_version_catalog_raises_on_write(self, tmp_project):
        """write_version also raises when version catalog is present."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text("[versions]\n")
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        with pytest.raises(VersionError, match="version catalog"):
            target.write_version(str(tmp_project), "2.0.0", None)

    def test_no_catalog_no_error(self, tmp_project):
        """No error when gradle/libs.versions.toml does not exist."""
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        version = target.read_version(str(tmp_project))
        assert version == "1.0.0"

    def test_no_catalog_with_gradle_dir(self, tmp_project):
        """No error when gradle/ dir exists but no libs.versions.toml."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        version = target.read_version(str(tmp_project))
        assert version == "1.0.0"

    def test_version_catalog_error_message_mentions_phase_8a(self, tmp_project):
        """Error message mentions Phase 8a for future support."""
        gradle_dir = tmp_project / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text("[versions]\n")
        (tmp_project / "build.gradle.kts").write_text('version = "1.0.0"\n')

        target = MavenTarget()
        with pytest.raises(VersionError, match="Phase 8a"):
            target.read_version(str(tmp_project))
