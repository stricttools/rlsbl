"""JavaScript and TypeScript linter using regex pattern matching as a fallback when tree-sitter is unavailable for npm package analysis."""

import json
import os
import re

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

_ALL_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs", ".tsx")

# Regex patterns for JS/TS source analysis
_ES_IMPORT_RE = re.compile(r"""import\s+.*\s+from\s+['"]([^'"]+)['"]""")
_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_EXPORT_FROM_RE = re.compile(r"""export\s+.*\s+from\s+['"]([^'"]+)['"]""")
_CONSOLE_RE = re.compile(r"\bconsole\.(log|warn|error|info)\s*\(")


def _check_forbidden_imports(lines, filepath, config):
    """Check each line for forbidden package imports."""
    results = []
    forbidden = frozenset(config.forbidden_imports)

    for lineno, line in enumerate(lines, start=1):
        for pattern in (_ES_IMPORT_RE, _REQUIRE_RE, _DYNAMIC_IMPORT_RE, _EXPORT_FROM_RE):
            m = pattern.search(line)
            if m:
                pkg = m.group(1)
                if pkg in forbidden:
                    results.append(LintResult(
                        file=filepath,
                        line=lineno,
                        rule="forbidden-import",
                        severity="error",
                        message=f"Library imports forbidden package '{pkg}'",
                    ))

    return results


def _check_stdout(lines, filepath, config):
    """Detect console.log/warn/error/info calls."""
    results = []
    ignore = set(config.stdout_ignore)

    if "console" in ignore:
        return results

    for lineno, line in enumerate(lines, start=1):
        m = _CONSOLE_RE.search(line)
        if m:
            method = m.group(1)
            results.append(LintResult(
                file=filepath,
                line=lineno,
                rule="stdout",
                severity="error",
                message=f"Library calls console.{method}()",
            ))

    return results


def _check_entry_points(project_path, config):
    """Check package.json for 'bin' field."""
    pkg_path = os.path.join(project_path, "package.json")
    if not os.path.isfile(pkg_path):
        return []

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    ignore = set(config.entry_point_ignore)
    results = []
    bin_field = data.get("bin")

    if isinstance(bin_field, str):
        name = data.get("name", "")
        if name not in ignore:
            results.append(LintResult(
                file=pkg_path,
                line=0,
                rule="entry-point",
                severity="error",
                message=f"Library declares CLI entry point '{name}'",
            ))
    elif isinstance(bin_field, dict):
        for name in bin_field:
            if name not in ignore:
                results.append(LintResult(
                    file=pkg_path,
                    line=0,
                    rule="entry-point",
                    severity="error",
                    message=f"Library declares CLI entry point '{name}'",
                ))

    return results


class NpmRegexLinter:
    """npm (JS/TS) linter using regex pattern matching."""

    language = "npm"
    parser_type = "regex"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        """Run forbidden-import and stdout checks on JS/TS files via regex."""
        results = []

        # Entry point check
        if config.entry_point_enabled:
            results.extend(_check_entry_points(project_path, config))

        # Source file checks
        for filepath in walk_source_files(project_path, _ALL_EXTENSIONS, config.exclude_patterns):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            lines = source.splitlines()
            results.extend(_check_forbidden_imports(lines, filepath, config))
            if config.stdout_enabled:
                results.extend(_check_stdout(lines, filepath, config))

        return results
