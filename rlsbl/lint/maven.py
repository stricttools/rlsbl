"""Maven/Gradle linter that delegates to the project's detected lint command (detekt, checkstyle, or ./gradlew check) via subprocess."""

import subprocess

from .config import LanguageLintConfig
from .result import LintResult


class MavenLinter:
    """Subprocess-based linter for Maven/Gradle projects.

    Delegates to MavenTarget.detect_lint_command() to determine the
    appropriate lint command, then runs it. LanguageLintConfig fields
    (forbidden_imports, stdout_enabled, etc.) are ignored since they
    are meaningless for subprocess linters.
    """

    language = "maven"
    parser_type = "subprocess"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        from ..targets.maven import MavenTarget

        cmd = MavenTarget.detect_lint_command(project_path)
        if cmd is None:
            return []

        result = subprocess.run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            return []

        # Combine stderr and stdout for the error message -- lint tools
        # may write diagnostics to either stream.
        output = (result.stderr or "") + (result.stdout or "")
        output = output.strip()
        if not output:
            output = f"lint command failed with exit code {result.returncode}"

        return [
            LintResult(
                file=project_path,
                line=0,
                rule="maven-lint",
                severity="error",
                message=output,
            )
        ]
