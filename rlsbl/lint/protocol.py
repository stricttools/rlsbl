"""Abstract protocols defining interfaces for per-language linters and import scanners."""

from typing import Protocol, runtime_checkable

from .config import LanguageLintConfig
from .result import LintResult


@runtime_checkable
class LanguageLinter(Protocol):
    language: str  # "python", "go", "npm"
    parser_type: str  # "ast" or "regex"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]: ...


@runtime_checkable
class ImportScanner(Protocol):
    def scan_imports(self, project_path: str) -> set[tuple[str, str, int]]:
        """Collect all imports from source files in a project.

        Returns a set of (package_name, file_path, line_number) tuples.
        """
        ...
