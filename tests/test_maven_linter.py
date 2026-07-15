"""Tests for MavenLinter -- subprocess-based lint for Maven/Gradle projects."""

import subprocess
from unittest.mock import patch, MagicMock

from rlsbl.lint import lint_library, _detect_languages, _create_linter
from rlsbl.lint.config import LanguageLintConfig
from rlsbl.lint.maven import MavenLinter
from rlsbl.lint.result import LintResult
from rlsbl.testing import CHECK_TIMEOUT_HINT


class TestMavenLinterLint:
    """MavenLinter.lint() delegates to detect_lint_command and subprocess."""

    def test_returns_empty_on_success(self, tmp_path):
        """Subprocess exits 0 -> empty list."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "detekt"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            results = linter.lint(str(tmp_path), config)

        assert results == []
        mock_run.assert_called_once_with(
            ["./gradlew", "detekt"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_timeout_message_includes_budget_hint(self, tmp_path):
        """A lint timeout surfaces the configurable-budget remediation hint."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "detekt"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["./gradlew", "detekt"], timeout=1),
            ),
        ):
            results = linter.lint(str(tmp_path), config)

        assert len(results) == 1
        assert "timed out" in results[0].message
        assert CHECK_TIMEOUT_HINT in results[0].message

    def test_returns_lint_result_on_failure(self, tmp_path):
        """Subprocess exits non-zero -> one LintResult with error output."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "detekt found 3 issues\n"
        mock_result.stdout = ""

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "detekt"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                return_value=mock_result,
            ),
        ):
            results = linter.lint(str(tmp_path), config)

        assert len(results) == 1
        r = results[0]
        assert r.rule == "maven-lint"
        assert r.severity == "error"
        assert "detekt found 3 issues" in r.message

    def test_returns_empty_when_no_gradlew(self, tmp_path):
        """detect_lint_command returns None -> empty list (no wrapper found)."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        with patch(
            "rlsbl.targets.maven.MavenTarget.detect_lint_command",
            return_value=None,
        ):
            results = linter.lint(str(tmp_path), config)

        assert results == []

    def test_failure_with_empty_output(self, tmp_path):
        """Non-zero exit with no output -> synthetic message with exit code."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stderr = ""
        mock_result.stdout = ""

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "check"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                return_value=mock_result,
            ),
        ):
            results = linter.lint(str(tmp_path), config)

        assert len(results) == 1
        assert "exit code 2" in results[0].message


class TestDetectLanguagesMaven:
    """_detect_languages picks up maven from Gradle/Maven build files."""

    def test_detects_from_build_gradle_kts(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text('group = "com.example"\n')
        languages = _detect_languages(str(tmp_path))
        assert "maven" in languages

    def test_detects_from_build_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("group 'com.example'\n")
        languages = _detect_languages(str(tmp_path))
        assert "maven" in languages

    def test_detects_from_pom_xml(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>\n")
        languages = _detect_languages(str(tmp_path))
        assert "maven" in languages

    def test_does_not_detect_without_build_files(self, tmp_path):
        (tmp_path / "README.md").write_text("hello\n")
        languages = _detect_languages(str(tmp_path))
        assert "maven" not in languages


class TestCreateLinterMaven:
    """_create_linter returns MavenLinter for 'maven' regardless of parser_type."""

    def test_create_linter_maven_ast(self):
        linter = _create_linter("maven", "ast")
        assert isinstance(linter, MavenLinter)

    def test_create_linter_maven_regex(self):
        linter = _create_linter("maven", "regex")
        assert isinstance(linter, MavenLinter)

    def test_attributes(self):
        linter = MavenLinter()
        assert linter.language == "maven"
        assert linter.parser_type == "subprocess"


class TestLintLibraryMavenIntegration:
    """lint_library() integration test for a maven project."""

    def test_lint_library_maven_success(self, tmp_path):
        """lint_library on a gradle project with passing lint returns empty."""
        (tmp_path / "build.gradle.kts").write_text('group = "com.example"\n')

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "check"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                return_value=mock_result,
            ),
        ):
            results = lint_library(str(tmp_path))

        assert results == []

    def test_lint_library_maven_failure(self, tmp_path):
        """lint_library on a gradle project with failing lint returns results."""
        (tmp_path / "build.gradle.kts").write_text('group = "com.example"\n')

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "checkstyle violations found"
        mock_result.stdout = ""

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "checkstyleMain"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                return_value=mock_result,
            ),
        ):
            results = lint_library(str(tmp_path))

        assert len(results) == 1
        assert results[0].rule == "maven-lint"
        assert "checkstyle violations found" in results[0].message
