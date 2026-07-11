"""Multi-language library boundary linter that detects accidental external exposure of internal symbols across Python, Go, and npm libraries to keep public APIs clean and intentional.

Public API:
    lint_library(project_path) -> list[LintResult]
    scan_imports(project_path) -> set  (ImportRecord or tuples)
"""

import os

from .config import (
    LanguageLintConfig,
    load_language_config,
    load_parser_setting,
)
from .result import LintResult

__all__ = ["lint_library", "scan_imports"]

# Default exclusion patterns for library lint, per language.
# These exclude test and example files from lint by default so that
# library boundary checks focus on production code only.
_DEFAULT_EXCLUDE_PATTERNS: dict[str, list[str]] = {
    "python": ["tests/", "test_*.py", "conftest.py", "examples/"],
    "go": ["*_test.go", "examples/"],
    "npm": ["__tests__/", "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts", "examples/"],
}


def _detect_languages(project_path: str) -> list[str]:
    """Detect which languages are present in the project."""
    languages = []
    if os.path.isfile(os.path.join(project_path, "pyproject.toml")):
        languages.append("python")
    if os.path.isfile(os.path.join(project_path, "go.mod")):
        languages.append("go")
    if os.path.isfile(os.path.join(project_path, "package.json")):
        languages.append("npm")
    if (
        os.path.isfile(os.path.join(project_path, "build.gradle.kts"))
        or os.path.isfile(os.path.join(project_path, "build.gradle"))
        or os.path.isfile(os.path.join(project_path, "pom.xml"))
    ):
        languages.append("maven")
    return languages


def _create_linter(language: str, parser_type: str):
    """Create the appropriate linter instance for a language and parser type."""
    if language == "python":
        if parser_type == "regex":
            from .python_regex import PythonRegexLinter
            return PythonRegexLinter()
        from .python_ast import PythonAstLinter
        return PythonAstLinter()
    if language == "go":
        if parser_type == "regex":
            from .go_regex import GoRegexLinter
            return GoRegexLinter()
        from .go_ast import GoAstLinter
        return GoAstLinter()
    if language == "npm":
        if parser_type == "regex":
            from .npm_regex import NpmRegexLinter
            return NpmRegexLinter()
        from .npm_ast import NpmAstLinter
        return NpmAstLinter()
    if language == "maven":
        from .maven import MavenLinter
        return MavenLinter()
    return None


def _create_import_scanner(language: str):
    """Create an AST-based import scanner for a language.

    Import scanning always uses the AST parser (not regex) since it needs
    accurate import extraction.
    """
    if language == "python":
        from .python_ast import PythonAstLinter
        return PythonAstLinter()
    if language == "npm":
        from .npm_ast import NpmAstLinter
        return NpmAstLinter()
    return None


def lint_library(
    project_path: str,
    *,
    allowed_imports: list[str] | None = None,
    check_timeout: int | None = None,
    releasable_lint_dir: str | None = None,
) -> list[LintResult]:
    """Analyze a project for library boundary violations.

    Detects languages present in the project, loads per-language config,
    and runs the appropriate linter for each.

    Args:
        project_path: path to the project root directory.
        allowed_imports: optional list of imports to allow (merged with
            per-project TOML allow-list). Typically from workspace.toml
            ``lint_allow``.
        check_timeout: optional subprocess timeout in seconds. Passed to
            linters that shell out (e.g. MavenLinter). Defaults to 120
            when None.
        releasable_lint_dir: optional path to the releasable-level ``lint/``
            directory. When the member has no per-language lint config of its
            own, the releasable-level config in this directory is used.

    Returns a list of LintResult namedtuples.
    """
    project_path = os.path.abspath(project_path)
    parser_type = load_parser_setting(project_path)
    languages = _detect_languages(project_path)

    if not languages:
        return []

    results: list[LintResult] = []
    for language in languages:
        config = load_language_config(
            project_path, language, releasable_lint_dir=releasable_lint_dir
        )
        # Merge workspace-level allowed imports with per-project TOML allow-list
        if allowed_imports:
            config.allowed_imports = list(set(config.allowed_imports) | set(allowed_imports))
        # Subtract allowed imports from forbidden imports
        if config.allowed_imports:
            allowed_set = set(config.allowed_imports)
            config.forbidden_imports = [m for m in config.forbidden_imports if m not in allowed_set]
        # Merge default test/example exclusions with user-configured ones
        defaults = _DEFAULT_EXCLUDE_PATTERNS.get(language, [])
        merged = list(dict.fromkeys(defaults + config.exclude_patterns))
        config.exclude_patterns = merged
        linter = _create_linter(language, parser_type)
        if linter is not None:
            if check_timeout is not None and hasattr(linter, "parser_type") and linter.parser_type == "subprocess":
                results.extend(linter.lint(project_path, config, check_timeout=check_timeout))
            else:
                results.extend(linter.lint(project_path, config))

    return results


def scan_imports(project_path: str) -> set:
    """Collect all imports from source files in a project.

    Detects languages present and uses AST-based scanners to extract
    every import statement found.

    Args:
        project_path: path to the project root directory.

    Returns a set of import records. Python imports are ImportRecord
    dataclasses (top_level, full_path, filepath, line, guarded, type_checking).
    npm imports are (package_name, file_path, line_number, guarded) tuples.
    """
    project_path = os.path.abspath(project_path)
    languages = _detect_languages(project_path)

    if not languages:
        return set()

    all_imports: set = set()
    for language in languages:
        scanner = _create_import_scanner(language)
        if scanner is not None:
            all_imports.update(scanner.scan_imports(project_path))

    return all_imports
