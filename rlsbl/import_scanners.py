"""Python, Dart, and npm import scanners for dependency-import validation.

Filters raw import data to workspace-relevant imports, handles
language-specific edge cases, and distinguishes lib/ vs test/ contexts.
"""

import os
import re
import sys
from dataclasses import dataclass

from .lint.npm_ast import NpmAstLinter
from .lint.python_ast import PythonAstLinter
from .lint.utils import walk_source_files
from .targets.utils import normalize_pypi

# Python 3.10+ provides this; used to exclude stdlib imports.
_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)

# Directories that indicate test context.
_TEST_DIRS = frozenset({"test", "tests", "__tests__"})

# Directories that indicate example/demo context (not production code).
_EXAMPLE_DIRS = frozenset({"examples", "example"})

# File name patterns that indicate test files (checked against basename).
_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^.*_test\.go$"),
    re.compile(r"^.*\.test\.[jt]sx?$"),
    re.compile(r"^.*\.spec\.[jt]sx?$"),
    re.compile(r"^conftest\.py$"),
)

# Shared constant for non-production context detection. Reusable by lint
# exclusion and dead-module checks.
_NON_PRODUCTION_PATTERNS = {
    "test_dirs": _TEST_DIRS,
    "example_dirs": _EXAMPLE_DIRS,
    "test_file_patterns": _TEST_FILE_PATTERNS,
}

# Regex for Dart package imports: import 'package:foo/bar.dart'
# Also matches export statements.
_DART_PACKAGE_RE = re.compile(r"""(?:import|export)\s+['"]package:(\w+)/""")


@dataclass(frozen=True)
class ImportInfo:
    """A single workspace-relevant import detected in a source file."""

    package_name: str
    file_path: str
    line_number: int
    is_test_context: bool


def _is_test_context(filepath: str, project_path: str) -> bool:
    """Determine whether a file is in a non-production context.

    Checks directory names (test, tests, __tests__, examples, example)
    and file name patterns (test_*.py, *_test.go, *.spec.ts, etc.).
    """
    rel = os.path.relpath(filepath, project_path)
    parts = rel.split(os.sep)
    non_prod_dirs = _TEST_DIRS | _EXAMPLE_DIRS
    if any(part in non_prod_dirs for part in parts):
        return True
    basename = parts[-1]
    return any(pat.match(basename) for pat in _TEST_FILE_PATTERNS)


class PythonImportScanner:
    """Scan Python source files for workspace-relevant imports.

    Uses the AST-based scanner from the lint system, then post-processes
    to filter out stdlib, relative imports, and non-workspace packages.
    """

    def scan(
        self,
        project_path: str,
        workspace_names: set[str],
    ) -> list[ImportInfo]:
        """Scan project_path for Python imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names
                (as they appear in pyproject.toml, e.g. "my-lib").

        Returns:
            list of ImportInfo for imports that match workspace members.
        """
        project_path = os.path.abspath(project_path)

        # Build normalized lookup: normalize_pypi(name) -> original name
        normalized_lookup = {
            normalize_pypi(name): name for name in workspace_names
        }

        linter = PythonAstLinter()
        raw_imports = linter.scan_imports(project_path)

        results = []
        for pkg_name, filepath, line_number in raw_imports:
            # Skip empty names (relative imports produce empty top-level)
            if not pkg_name:
                continue

            # Skip relative imports that start with a dot
            if pkg_name.startswith("."):
                continue

            # Skip stdlib modules
            if pkg_name in _STDLIB_MODULES:
                continue

            # Map import name to PyPI-normalized form and check workspace
            normalized = normalize_pypi(pkg_name)
            if normalized in normalized_lookup:
                results.append(ImportInfo(
                    package_name=normalized_lookup[normalized],
                    file_path=filepath,
                    line_number=line_number,
                    is_test_context=_is_test_context(filepath, project_path),
                ))

        return results


class DartImportScanner:
    """Scan Dart source files for workspace-relevant package imports.

    Uses regex to extract package names from import/export statements.
    Checks for missing generated (.g.dart) files when build_runner is
    configured.
    """

    def scan(
        self,
        project_path: str,
        workspace_names: set[str],
    ) -> list[ImportInfo]:
        """Scan project_path for Dart imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names
                (as they appear in pubspec.yaml).

        Returns:
            list of ImportInfo for imports that match workspace members.

        Raises:
            RuntimeError: if build.yaml exists but no .g.dart files
                are found in the project (missing code generation).
        """
        project_path = os.path.abspath(project_path)

        self._check_generated_files(project_path)

        dart_files = walk_source_files(project_path, (".dart",), [])

        results = []
        for filepath in dart_files:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except (OSError, UnicodeDecodeError):
                continue

            is_test = _is_test_context(filepath, project_path)

            for i, line in enumerate(lines, start=1):
                match = _DART_PACKAGE_RE.search(line)
                if match:
                    pkg_name = match.group(1)
                    if pkg_name in workspace_names:
                        results.append(ImportInfo(
                            package_name=pkg_name,
                            file_path=filepath,
                            line_number=i,
                            is_test_context=is_test,
                        ))

        return results

    def _check_generated_files(self, project_path: str) -> None:
        """Raise RuntimeError if build_runner is configured but no .g.dart files exist."""
        build_yaml = os.path.join(project_path, "build.yaml")
        if not os.path.isfile(build_yaml):
            return

        # Walk the project looking for at least one .g.dart file
        for dirpath, dirs, filenames in os.walk(project_path):
            # Skip hidden dirs and build output
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in ("build", "node_modules")
            ]
            for filename in filenames:
                if filename.endswith(".g.dart"):
                    return

        raise RuntimeError(
            f"build.yaml exists in {project_path} but no .g.dart files found. "
            "Run the build_runner code generator (e.g. 'dart run build_runner build')."
        )


# Node.js built-in modules to exclude from npm import scanning.
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})


def _extract_npm_bare_name(specifier: str) -> str | None:
    """Extract bare package name from an npm import specifier.

    Returns None for relative imports, Node.js builtins, and
    node:-prefixed builtins. For scoped packages (@scope/pkg/foo),
    returns @scope/pkg. For unscoped (pkg/foo), returns pkg.
    """
    # Skip relative imports
    if specifier.startswith(".") or specifier.startswith("/"):
        return None

    # Strip node: prefix and skip builtins
    bare = specifier.removeprefix("node:")
    if bare in _NODE_BUILTINS:
        return None
    # node: prefix with subpath (e.g. node:fs/promises)
    if specifier.startswith("node:"):
        return None

    # Scoped package: @scope/pkg or @scope/pkg/subpath
    if specifier.startswith("@"):
        parts = specifier.split("/")
        if len(parts) < 2:
            # Malformed scoped import (just @scope)
            return None
        return f"{parts[0]}/{parts[1]}"

    # Unscoped: pkg or pkg/subpath
    return specifier.split("/")[0]


class NpmImportScanner:
    """Scan JS/TS source files for workspace-relevant imports.

    Uses the AST-based scanner from the npm lint system, then
    post-processes to filter out relative imports, Node.js builtins,
    and non-workspace packages.
    """

    def scan(
        self,
        project_path: str,
        workspace_names: set[str],
    ) -> list[ImportInfo]:
        """Scan project_path for JS/TS imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names
                (as they appear in package.json, e.g. "@scope/my-lib").

        Returns:
            list of ImportInfo for imports that match workspace members.
        """
        project_path = os.path.abspath(project_path)

        # Build normalized lookup: lowercase name -> original name
        normalized_lookup = {
            name.lower(): name for name in workspace_names
        }

        linter = NpmAstLinter()
        raw_imports = linter.scan_imports(project_path)

        results = []
        for specifier, filepath, line_number in raw_imports:
            bare = _extract_npm_bare_name(specifier)
            if bare is None:
                continue

            # npm names are case-insensitive
            normalized = bare.lower()
            if normalized in normalized_lookup:
                results.append(ImportInfo(
                    package_name=normalized_lookup[normalized],
                    file_path=filepath,
                    line_number=line_number,
                    is_test_context=_is_test_context(filepath, project_path),
                ))

        return results
