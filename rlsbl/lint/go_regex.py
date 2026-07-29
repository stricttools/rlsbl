"""Go linter using regex pattern matching as a fallback when tree-sitter is unavailable, providing the same checks via line-oriented patterns."""

import re

from .config import LanguageLintConfig
from .result import LintResult
from .utils import walk_source_files

# Regex patterns for Go source analysis
_SINGLE_IMPORT_RE = re.compile(r'^import\s+"([^"]+)"')
_GROUPED_IMPORT_RE = re.compile(r"import\s*\(([\s\S]*?)\)", re.MULTILINE)
_GROUPED_IMPORT_PATH_RE = re.compile(r'"([^"]+)"')
_FMT_PRINT_RE = re.compile(r"\bfmt\.(Print|Printf|Println)\s*\(")
_OS_STDOUT_WRITE_RE = re.compile(r"\bos\.Stdout\.Write\s*\(")
_PACKAGE_MAIN_RE = re.compile(r"^package\s+main\b", re.MULTILINE)
_FUNC_MAIN_RE = re.compile(r"^func\s+main\s*\(", re.MULTILINE)


def _check_forbidden_imports(source, lines, filepath, config):
    """Check for forbidden Go package imports."""
    results = []
    forbidden = frozenset(config.forbidden_imports)

    # Single imports (line-by-line)
    for lineno, line in enumerate(lines, start=1):
        m = _SINGLE_IMPORT_RE.match(line)
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

    # Grouped imports (multi-line)
    for m in _GROUPED_IMPORT_RE.finditer(source):
        block = m.group(1)
        # Calculate the starting line of the import block
        block_start = source[:m.start(1)].count("\n") + 1
        for i, block_line in enumerate(block.splitlines()):
            pm = _GROUPED_IMPORT_PATH_RE.search(block_line)
            if pm:
                pkg = pm.group(1)
                if pkg in forbidden:
                    results.append(LintResult(
                        file=filepath,
                        line=block_start + i,
                        rule="forbidden-import",
                        severity="error",
                        message=f"Library imports forbidden package '{pkg}'",
                    ))

    return results


def _check_stdout(lines, filepath, config):
    """Detect fmt.Print* and os.Stdout.Write() calls."""
    results = []
    ignore = set(config.stdout_ignore)

    for lineno, line in enumerate(lines, start=1):
        # fmt.Print, fmt.Printf, fmt.Println
        if "fmt" not in ignore:
            m = _FMT_PRINT_RE.search(line)
            if m:
                fname = m.group(1)
                results.append(LintResult(
                    file=filepath,
                    line=lineno,
                    rule="stdout",
                    severity="error",
                    message=f"Library calls fmt.{fname}()",
                ))

        # os.Stdout.Write
        if "os" not in ignore and _OS_STDOUT_WRITE_RE.search(line):
            results.append(LintResult(
                file=filepath,
                line=lineno,
                rule="stdout",
                severity="error",
                message="Library writes to os.Stdout",
            ))

    return results


def _check_entry_points(source, filepath, config):
    """Detect func main() in package main files."""
    results = []
    ignore = set(config.entry_point_ignore)

    if "main" in ignore:
        return results

    if _PACKAGE_MAIN_RE.search(source):
        m = _FUNC_MAIN_RE.search(source)
        if m:
            lineno = source[:m.start()].count("\n") + 1
            results.append(LintResult(
                file=filepath,
                line=lineno,
                rule="entry-point",
                severity="error",
                message="Library declares entry point func main()",
            ))

    return results


class GoRegexLinter:
    """Go linter using regex pattern matching."""

    language = "go"
    parser_type = "regex"

    def lint(self, project_path: str, config: LanguageLintConfig) -> list[LintResult]:
        """Run forbidden-import, stdout, and entry-point checks on Go files via regex."""
        results = []

        for filepath in walk_source_files(project_path, (".go",), config.exclude_patterns):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            lines = source.splitlines()
            results.extend(_check_forbidden_imports(source, lines, filepath, config))
            if config.stdout_enabled:
                results.extend(_check_stdout(lines, filepath, config))
            if config.entry_point_enabled:
                results.extend(_check_entry_points(source, filepath, config))

        return results
