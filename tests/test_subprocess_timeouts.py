"""Tests for subprocess timeout handling in MavenLinter and testing.py."""

import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.lint.config import LanguageLintConfig
from rlsbl.lint.maven import MavenLinter
from rlsbl.lint.result import LintResult
from rlsbl.testing import run_project_tests


# ---------------------------------------------------------------------------
# Phase 2a: MavenLinter timeout tests
# ---------------------------------------------------------------------------


class TestMavenLinterTimeout:
    """MavenLinter.lint() handles subprocess.TimeoutExpired."""

    def test_timeout_returns_lint_result(self, tmp_path):
        """When subprocess.run raises TimeoutExpired, returns a single LintResult with 'timeout' in message."""
        linter = MavenLinter()
        config = LanguageLintConfig()

        with (
            patch(
                "rlsbl.targets.maven.MavenTarget.detect_lint_command",
                return_value=["./gradlew", "detekt"],
            ),
            patch(
                "rlsbl.lint.maven.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["./gradlew", "detekt"], timeout=120
                ),
            ),
        ):
            results = linter.lint(str(tmp_path), config)

        assert len(results) == 1
        r = results[0]
        assert isinstance(r, LintResult)
        assert r.rule == "maven-lint"
        assert r.severity == "error"
        assert "timed out" in r.message.lower()

    def test_subprocess_run_called_with_timeout(self, tmp_path):
        """subprocess.run is called with timeout=120."""
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
            linter.lint(str(tmp_path), config)

        assert mock_run.call_args.kwargs.get("timeout") == 120


# ---------------------------------------------------------------------------
# Phase 2b: testing.py timeout tests
# ---------------------------------------------------------------------------


class TestMavenTestsTimeout:
    """_run_maven_tests handles subprocess.TimeoutExpired."""

    def test_maven_gradlew_timeout_returns_false(self, tmp_path):
        """When ./gradlew test times out, run_project_tests returns False."""
        # Create a gradlew file so the gradlew path is chosen
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\n")
        os.chmod(str(gradlew), 0o755)

        with patch(
            "rlsbl.testing.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["./gradlew", "test"], timeout=120
            ),
        ):
            result = run_project_tests("maven", project_dir=str(tmp_path))

        assert result is False


class TestPypiTestsTimeout:
    """_run_pypi_tests handles subprocess.TimeoutExpired."""

    def test_pypi_pytest_timeout_returns_false(self, tmp_path):
        """When uv run pytest times out, run_project_tests returns False."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "0.1.0"\n\n'
            '[dependency-groups]\ndev = ["pytest>=8.0"]\n'
        )
        with (
            patch("rlsbl.testing.require_tool") as mock_tool,
            patch("rlsbl.testing.detect_uv_workspace_root", return_value=None),
            patch(
                "rlsbl.testing.subprocess.run",
            ) as mock_run,
        ):
            mock_tool.return_value = "/usr/bin/uv"
            # Standalone: single call (uv run pytest) times out
            mock_run.side_effect = [
                subprocess.TimeoutExpired(
                    cmd=["uv", "run", "pytest"], timeout=120
                ),
            ]

            result = run_project_tests("pypi", project_dir=str(tmp_path))

        assert result is False


class TestGoTestsTimeout:
    """_run_go_tests handles subprocess.TimeoutExpired."""

    def test_go_timeout_returns_false(self, tmp_path):
        """When go test times out, run_project_tests returns False."""
        with patch(
            "rlsbl.testing.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["go", "test", "./...", "-race", "-short", "-count=1"],
                timeout=120,
            ),
        ):
            result = run_project_tests("go", project_dir=str(tmp_path))

        assert result is False
