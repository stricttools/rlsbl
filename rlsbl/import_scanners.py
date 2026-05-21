"""Python and Dart import scanners for dependency-import validation.

Filters raw import data to workspace-relevant imports, handles
language-specific edge cases, and distinguishes lib/ vs test/ contexts.
"""

import os
import re
import sys
from dataclasses import dataclass

from .lint.python_ast import PythonAstLinter
from .lint.utils import walk_source_files
from .targets.utils import normalize_pypi

# Python 3.10+ provides this; used to exclude stdlib imports.
_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)

# Directories that indicate test context.
_TEST_DIRS = frozenset({"test", "tests"})

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
    """Determine whether a file is in a test directory.

    Checks if any path component between project_path and filepath
    is 'test' or 'tests'.
    """
    rel = os.path.relpath(filepath, project_path)
    parts = rel.split(os.sep)
    return any(part in _TEST_DIRS for part in parts)


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
