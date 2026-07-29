"""Python linter using regex pattern matching as a fallback when tree-sitter is unavailable, providing the same checks via line patterns."""

import os
import re
import tomllib

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

# Regex patterns for source analysis (anchored to reduce false positives in strings)
_IMPORT_RE = re.compile(r"^import\s+(\w+)")
_FROM_IMPORT_RE = re.compile(r"^from\s+(\w+)")
_PRINT_RE = re.compile(r"\bprint\s*\(")
_SYS_WRITE_RE = re.compile(r"\bsys\.(stdout|stderr)\.write\s*\(")
_LOGGING_RE = re.compile(r"\blogging\.\w+\s*\(")


def _check_forbidden_imports(lines, filepath, config):
    """Check each line for forbidden module imports."""
    results = []
    forbidden = frozenset(config.forbidden_imports)

    for lineno, line in enumerate(lines, start=1):
        m = _IMPORT_RE.match(line)
        if m:
            module = m.group(1)
            if module in forbidden:
                results.append(LintResult(
                    file=filepath,
                    line=lineno,
                    rule="forbidden-import",
                    severity="error",
                    message=f"Library imports interface module '{module}'",
                ))
            continue

        m = _FROM_IMPORT_RE.match(line)
        if m:
            module = m.group(1)
            if module in forbidden:
                results.append(LintResult(
                    file=filepath,
                    line=lineno,
                    rule="forbidden-import",
                    severity="error",
                    message=f"Library imports interface module '{module}'",
                ))

    return results


def _check_stdout(lines, filepath, config):
    """Detect print(), sys.stdout/stderr.write(), and logging.* calls."""
    results = []
    ignore = set(config.stdout_ignore)

    for lineno, line in enumerate(lines, start=1):
        # print() calls
        if "print" not in ignore and _PRINT_RE.search(line):
            results.append(LintResult(
                file=filepath,
                line=lineno,
                rule="stdout",
                severity="error",
                message="Library calls print()",
            ))

        # sys.stdout.write() / sys.stderr.write()
        if "sys" not in ignore:
            m = _SYS_WRITE_RE.search(line)
            if m:
                stream = m.group(1)
                results.append(LintResult(
                    file=filepath,
                    line=lineno,
                    rule="stdout",
                    severity="error",
                    message=f"Library writes to sys.{stream}",
                ))

        # logging.* calls
        if "logging" not in ignore and _LOGGING_RE.search(line):
            results.append(LintResult(
                file=filepath,
                line=lineno,
                rule="stdout",
                severity="warning",
                message="Library uses logging directly",
            ))

    return results


def _check_entry_points(project_path, config):
    """Check pyproject.toml for CLI entry point declarations."""
    pyproject_path = os.path.join(project_path, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return []

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []

    ignore = set(config.entry_point_ignore)
    results = []
    project = data.get("project", {})
    for section_key in ("scripts", "gui-scripts"):
        entries = project.get(section_key, {})
        for name in entries:
            if name not in ignore:
                results.append(LintResult(
                    file=pyproject_path,
                    line=0,
                    rule="entry-point",
                    severity="error",
                    message=f"Library declares CLI entry point '{name}'",
                ))
    return results


class PythonRegexLinter:
    """Python linter using regex pattern matching."""

    language = "python"
    parser_type = "regex"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        """Run forbidden-import and stdout checks on Python files via regex."""
        results = []

        # Entry point check
        if config.entry_point_enabled:
            results.extend(_check_entry_points(project_path, config))

        # Source file checks
        for filepath in walk_source_files(project_path, (".py",), config.exclude_patterns):
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
