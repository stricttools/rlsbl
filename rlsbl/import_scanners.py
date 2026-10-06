"""Python, npm, and Go import scanners for dependency-import validation.

Filters raw import data to workspace-relevant imports, handles
language-specific edge cases, and distinguishes lib/ vs test/ contexts.
"""

import os
import re
import sys
from dataclasses import dataclass

from .errors import VersionError
from .lint.go_ast import scan_imports as _go_scan_imports
from .lint.npm_ast import NpmAstLinter
from .lint.python_ast import PythonAstLinter
from .lint.utils import walk_source_files
from .module_paths import GO_SEP, dotted_under_module, owning_module
from .targets.utils import detect_python_package_root, normalize_pypi
from .utils import read_go_module_path

# Python 3.10+ provides this; used to exclude stdlib imports.
_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)

# Layer 1: Directories that ALWAYS indicate test context at any depth.
_ALWAYS_TEST_DIRS = frozenset({"__tests__", "testdata"})

# Layer 2: Directories that indicate test/example context only as the
# first path component (relative to project root).
_ROOT_TEST_DIRS = frozenset({
    "test", "tests", "example", "examples", "integration_test",
})

# File name patterns that indicate test files (checked against basename).
_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^.*_test\.go$"),
    re.compile(r"^.*\.test\.[jt]sx?$"),
    re.compile(r"^.*\.spec\.[jt]sx?$"),
    re.compile(r"^conftest\.py$"),
)

@dataclass(frozen=True)
class ImportInfo:
    """A single workspace-relevant import detected in a source file."""

    package_name: str
    file_path: str
    line_number: int
    is_test_context: bool
    guarded: bool = False
    type_checking: bool = False


def _is_test_context(filepath: str, project_path: str) -> bool:
    """Determine whether a file is in a non-production context.

    Uses a layered approach to avoid false positives for production
    paths that happen to contain directory names like "test":

    Layer 1 -- Unconditional directories (match at any depth):
        __tests__/, testdata/

    Layer 2 -- Root-relative directories (match only as first component):
        test/, tests/, example/, examples/, integration_test/

    Layer 3 -- File name patterns (checked against basename):
        test_*.py, *_test.py, *_test.go,
        *.test.[jt]sx?, *.spec.[jt]sx?, conftest.py
    """
    rel = os.path.relpath(filepath, project_path)
    parts = rel.split(os.sep)

    # Layer 1: unconditional directory patterns (any depth).
    # Check all directory components (exclude the filename itself).
    if any(part in _ALWAYS_TEST_DIRS for part in parts[:-1]):
        return True

    # Layer 2: root-relative test/example directories (first component only).
    if parts[0] in _ROOT_TEST_DIRS:
        return True

    # Layer 3: file name patterns.
    basename = parts[-1]
    return any(pat.match(basename) for pat in _TEST_FILE_PATTERNS)


def build_namespace_map(projects, workspace_root: str) -> dict[str, str]:
    """Map namespace-qualified import paths to workspace project names.

    For a project named 'protocols' at 'protocols/src/orxt/protocols/',
    returns {'orxt.protocols': 'protocols'}.

    Algorithm:
    1. For each project, call detect_python_package_root() to get the
       package root (e.g., 'src/orxt')
    2. The namespace is the package root's leaf directory name (e.g., 'orxt')
    3. Walk subdirectories of the package root looking for the project's
       directory name
    4. If src/orxt/protocols/ exists and project name is 'protocols',
       map 'orxt.protocols' -> 'protocols'
    """
    namespace_map: dict[str, str] = {}

    for proj in projects:
        proj_name = proj["name"] if isinstance(proj, dict) else proj.name
        proj_path = proj["path"] if isinstance(proj, dict) else proj.path
        project_dir = os.path.join(workspace_root, proj_path)

        try:
            pkg_root = detect_python_package_root(project_dir)
        except VersionError:
            continue
        if not pkg_root:
            continue

        # The namespace is the leaf directory of the package root
        # e.g., 'src/orxt' -> namespace is 'orxt'
        namespace = os.path.basename(pkg_root)
        if not namespace:
            continue

        # Build the absolute path to the package root
        abs_pkg_root = os.path.join(project_dir, pkg_root)
        if not os.path.isdir(abs_pkg_root):
            continue

        # Normalize the project name for filesystem matching
        proj_name_underscored = proj_name.replace("-", "_")

        # Walk immediate subdirectories of the package root looking for
        # the project's directory name
        try:
            entries = os.listdir(abs_pkg_root)
        except OSError:
            continue

        for entry in entries:
            entry_path = os.path.join(abs_pkg_root, entry)
            if not os.path.isdir(entry_path):
                continue
            if entry == proj_name or entry == proj_name_underscored:
                # Found: namespace.project_name -> project_name
                import_path = f"{namespace}.{entry}"
                namespace_map[import_path] = proj_name
                break

    return namespace_map


class PythonImportScanner:
    """Scan Python source files for workspace-relevant imports.

    Uses the AST-based scanner from the lint system, then post-processes
    to filter out stdlib, relative imports, and non-workspace packages.
    Supports namespace package detection via namespace_map and import_names.
    """

    def scan(
        self,
        project_path: str,
        workspace_names: set[str],
        exclude_dirs: list[str] | None = None,
        *,
        namespace_map: dict[str, str] | None = None,
        import_names: dict[str, str] | None = None,
    ) -> list[ImportInfo]:
        """Scan project_path for Python imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names
                (as they appear in pyproject.toml, e.g. "my-lib").
            exclude_dirs: directory paths to skip during the walk
                (relative to project_path or absolute).
            namespace_map: mapping of namespace-qualified import paths
                to workspace project names (e.g., {'orxt.protocols': 'protocols'}).
                Built by build_namespace_map().
            import_names: mapping of project_name -> import_name from workspace
                config. Used for explicit import_name overrides.

        Returns:
            list of ImportInfo for imports that match workspace members.
        """
        project_path = os.path.abspath(project_path)

        # Build normalized lookup: normalize_pypi(name) -> original name
        normalized_lookup = {
            normalize_pypi(name): name for name in workspace_names
        }

        # Build reverse import_name lookup: import_name -> project_name
        import_name_lookup: dict[str, str] = {}
        if import_names:
            for proj_name, imp_name in import_names.items():
                if imp_name:
                    import_name_lookup[imp_name] = proj_name

        # Sort namespace_map keys by length descending for longest-prefix matching
        ns_keys_sorted: list[str] = []
        if namespace_map:
            ns_keys_sorted = sorted(namespace_map.keys(), key=len, reverse=True)

        linter = PythonAstLinter()
        raw_imports = linter.scan_imports(project_path, exclude_dirs=exclude_dirs)

        results = []
        for record in raw_imports:
            top_level = record.top_level
            full_path = record.full_path

            # Skip empty names (relative imports produce empty top-level)
            if not top_level:
                continue

            # Skip relative imports that start with a dot
            if top_level.startswith("."):
                continue

            # Skip stdlib modules
            if top_level in _STDLIB_MODULES:
                continue

            matched_name = None

            # 1. Top-level match against workspace names (existing behavior)
            normalized = normalize_pypi(top_level)
            if normalized in normalized_lookup:
                matched_name = normalized_lookup[normalized]

            # 2. Check import_name overrides: if full_path starts with any import_name
            if matched_name is None and import_name_lookup:
                for imp_name, proj_name in import_name_lookup.items():
                    if dotted_under_module(full_path, imp_name):
                        matched_name = proj_name
                        break

            # 3. Longest-prefix match against namespace_map using full_path
            if matched_name is None and ns_keys_sorted:
                for ns_key in ns_keys_sorted:
                    if dotted_under_module(full_path, ns_key):
                        matched_name = namespace_map[ns_key]
                        break

            # 4. Sub-component match: check each component of the dotted
            #    import path against workspace names. This catches namespace
            #    package imports like 'from orxt.protocols import Tool' where
            #    'protocols' is a workspace member under the 'orxt' namespace.
            if matched_name is None and "." in full_path:
                parts = full_path.split(".")
                for part in parts[1:]:  # skip top-level (already checked in step 1)
                    normalized_part = normalize_pypi(part)
                    if normalized_part in normalized_lookup:
                        matched_name = normalized_lookup[normalized_part]
                        break

            if matched_name is not None:
                results.append(ImportInfo(
                    package_name=matched_name,
                    file_path=record.filepath,
                    line_number=record.line,
                    is_test_context=_is_test_context(record.filepath, project_path),
                    guarded=record.guarded,
                    type_checking=record.type_checking,
                ))

        return results


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
        exclude_dirs: list[str] | None = None,
    ) -> list[ImportInfo]:
        """Scan project_path for JS/TS imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names
                (as they appear in package.json, e.g. "@scope/my-lib").
            exclude_dirs: directory paths to skip during the walk
                (relative to project_path or absolute).

        Returns:
            list of ImportInfo for imports that match workspace members.
        """
        project_path = os.path.abspath(project_path)

        # Build normalized lookup: lowercase name -> original name
        normalized_lookup = {
            name.lower(): name for name in workspace_names
        }

        linter = NpmAstLinter()
        raw_imports = linter.scan_imports(project_path, exclude_dirs=exclude_dirs)

        results = []
        for specifier, filepath, line_number, _guarded in raw_imports:
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


class GoImportScanner:
    """Scan Go source files for workspace-relevant imports.

    Uses the tree-sitter-based scanner from the Go lint system, then
    post-processes to filter to imports matching other workspace projects'
    Go module paths.
    """

    def scan(
        self,
        project_path: str,
        workspace_names: set[str],
        exclude_dirs: list[str] | None = None,
        *,
        module_path_map: dict[str, str] | None = None,
    ) -> list[ImportInfo]:
        """Scan project_path for Go imports matching workspace members.

        Args:
            project_path: absolute path to the project root.
            workspace_names: set of workspace member package names.
            exclude_dirs: directory paths to skip during the walk
                (relative to project_path or absolute).
            module_path_map: mapping of workspace project name to its
                Go module path (from go.mod). Only Go projects appear
                in this map. Required for Go import detection.

        Returns:
            list of ImportInfo for imports that match workspace members.
        """
        project_path = os.path.abspath(project_path)

        if not module_path_map:
            return []

        # This project's own module path: an import it owns is no dependency.
        own_module_path = read_go_module_path(project_path)

        # Reverse lookup, module_path -> workspace_name. The project's own
        # module stays in it: an import is attributed to the longest module
        # containing it, and a sub-package of a nested module must resolve to
        # that module, never to the module enclosing it.
        module_to_name: dict[str, str | None] = {
            mod_path: ws_name for ws_name, mod_path in module_path_map.items()
        }
        if own_module_path:
            module_to_name[own_module_path] = None

        if not any(name is not None for name in module_to_name.values()):
            return []

        go_files = walk_source_files(
            project_path, (".go",), [], exclude_dirs=exclude_dirs,
        )

        results = []
        for filepath in go_files:
            raw_imports = _go_scan_imports(filepath)
            is_test = _is_test_context(filepath, project_path)

            for import_path, _fp, line_number in raw_imports:
                matched_name = self._match_workspace_import(
                    import_path, module_to_name,
                )
                if matched_name is not None:
                    results.append(ImportInfo(
                        package_name=matched_name,
                        file_path=filepath,
                        line_number=line_number,
                        is_test_context=is_test,
                    ))

        return results

    @staticmethod
    def _match_workspace_import(
        import_path: str,
        module_to_name: dict[str, str | None],
    ) -> str | None:
        """The workspace sibling an import path belongs to, or ``None``.

        The longest containing module path owns the import
        (:func:`rlsbl.module_paths.owning_module`); an import owned by the
        scanning project's own module (mapped to ``None``) is no dependency.
        """
        owner = owning_module(import_path, module_to_name, sep=GO_SEP)
        return None if owner is None else module_to_name[owner]
