"""Multi-language library boundary linter that detects accidental external exposure of internal symbols across Python, Go, and npm libraries to keep public APIs clean and intentional.

Public API:
    lint_library(project_path) -> list[LintResult]

Import scanning is NOT here. ``rlsbl.import_scanners`` and
``rlsbl.dep_validation`` drive the per-language scanners directly -- the
linters' own ``scan_imports`` methods for Python and npm, and
``rlsbl.lint.go_ast.scan_imports`` per file for Go. A project-wide
``scan_imports`` used to sit in this module as a third way in; nothing in
production ever called it.
"""

import os

from .config import (
    LanguageLintConfig,
    load_language_config,
    load_parser_setting,
)
from .languages import LANGUAGES, get_language
from .result import LintResult

__all__ = ["lint_library"]


def _detect_languages(project_path: str) -> list[str]:
    """Detect which languages are present in the project.

    Derived from the LANGUAGES table's declared manifests, in table order.
    """
    return [lang.name for lang in LANGUAGES if lang.detect(project_path)]


def _create_linter(language: str, parser_type: str):
    """Create the linter for a language and parser type.

    Raises on an unknown language rather than returning None: a caller that
    got here with a language the table does not declare has a bug, and a
    silent None would turn it into a lint that reports nothing and passes.
    """
    return get_language(language).linter(parser_type)


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
        defaults = list(get_language(language).default_excludes)
        merged = list(dict.fromkeys(defaults + config.exclude_patterns))
        config.exclude_patterns = merged
        # _create_linter raises for an unknown language; a detected language
        # always has one, so there is no branch here that can silently lint
        # nothing.
        linter = _create_linter(language, parser_type)
        if check_timeout is not None and getattr(linter, "parser_type", None) == "subprocess":
            results.extend(linter.lint(project_path, config, check_timeout=check_timeout))
        else:
            results.extend(linter.lint(project_path, config))

    return results
