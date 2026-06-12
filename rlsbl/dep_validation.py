"""Dependency validation for monorepo workspaces.

Checks for unused declared dependencies and undeclared imports
across workspace projects. Uses import scanners to compare actual
source imports against manifest-declared dependencies.
"""

import json
import os
import re
import tomllib
from collections import deque
from dataclasses import dataclass

from .errors import ConfigError
from .import_scanners import (
    DartImportScanner,
    GoImportScanner,
    NpmImportScanner,
    PythonImportScanner,
    _is_test_context,
)
from .lint.go_ast import scan_imports as _go_scan_imports
from .lint.utils import walk_source_files
from .workspace import WORKSPACE_DIR


def load_dep_overrides(root: str) -> dict[tuple[str, str], str]:
    """Load dep-overrides.toml from the monorepo config directory.

    Returns a dict mapping (package, dep) to reason string.
    Raises ValueError if an entry is missing a required 'reason' field.
    Returns empty dict if the file does not exist.
    """
    path = os.path.join(root, WORKSPACE_DIR, "dep-overrides.toml")
    if not os.path.isfile(path):
        return {}

    with open(path, "rb") as f:
        data = tomllib.load(f)

    result: dict[tuple[str, str], str] = {}
    for i, entry in enumerate(data.get("unused_allowed", [])):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"unused_allowed[{i}] must be a table"
            )
        for key in ("package", "dep", "reason"):
            if key not in entry:
                raise ConfigError(
                    f"unused_allowed[{i}] missing required key '{key}'"
                )
            if not isinstance(entry[key], str):
                raise ConfigError(
                    f"unused_allowed[{i}].{key} must be a string"
                )
        if not entry["reason"].strip():
            raise ConfigError(
                f"unused_allowed[{i}].reason must not be empty"
            )
        result[(entry["package"], entry["dep"])] = entry["reason"]

    return result


def _get_imported_workspace_packages(
    project_dir: str,
    workspace_names: set[str],
    exclude_dirs: list[str] | None = None,
    *,
    module_path_map: dict[str, str] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Scan a project for workspace imports, split by context.

    Args:
        project_dir: absolute path to the project root.
        workspace_names: set of all workspace member package names.
        exclude_dirs: directory paths to skip during the walk.
        module_path_map: mapping of workspace project name to its Go
            module path (from go.mod). Passed through to GoImportScanner.

    Returns (lib_imports, test_imports, guarded_imports) where each is a
    set of workspace package names. Guarded imports are those inside
    try/except ImportError blocks -- they count as "used" (not unused)
    but should not trigger undeclared-dep errors.
    """
    lib_imports: set[str] = set()
    test_imports: set[str] = set()
    guarded_imports: set[str] = set()

    for scanner in (PythonImportScanner(), DartImportScanner(), NpmImportScanner()):
        try:
            results = scanner.scan(project_dir, workspace_names, exclude_dirs=exclude_dirs)
        except RuntimeError:
            # DartImportScanner raises RuntimeError for missing .g.dart;
            # skip gracefully for dep validation purposes.
            continue
        for info in results:
            if info.guarded:
                guarded_imports.add(info.package_name)
            elif info.is_test_context:
                test_imports.add(info.package_name)
            else:
                lib_imports.add(info.package_name)

    # GoImportScanner needs the module_path_map keyword argument
    go_scanner = GoImportScanner()
    go_results = go_scanner.scan(
        project_dir, workspace_names,
        exclude_dirs=exclude_dirs,
        module_path_map=module_path_map,
    )
    for info in go_results:
        if info.guarded:
            guarded_imports.add(info.package_name)
        elif info.is_test_context:
            test_imports.add(info.package_name)
        else:
            lib_imports.add(info.package_name)

    return lib_imports, test_imports, guarded_imports


def check_unused_deps(
    project_name: str,
    project_dir: str,
    manifest_deps: set[str],
    workspace_names: set[str],
    whitelist: dict[tuple[str, str], str],
    *,
    _cached_imports: tuple[set[str], set[str], set[str]] | None = None,
) -> list[str]:
    """Check for declared workspace deps that no source file imports.

    Args:
        project_name: name of the project being checked.
        project_dir: absolute path to the project directory.
        manifest_deps: set of declared intra-workspace dependency names.
        workspace_names: set of all workspace member package names.
        whitelist: mapping of (package, dep) -> reason for allowed unused deps.
        _cached_imports: optional pre-computed (lib_imports, test_imports,
            guarded_imports) tuple to avoid redundant scans when multiple
            checks share the same project.

    Returns:
        list of error strings (empty means all good).
    """
    if not manifest_deps:
        return []

    if _cached_imports is not None:
        lib_imports, test_imports, guarded_imports = _cached_imports
    else:
        lib_imports, test_imports, guarded_imports = _get_imported_workspace_packages(
            project_dir, workspace_names
        )
    # Guarded imports (try/except ImportError) count as used for unused check
    all_imports = lib_imports | test_imports | guarded_imports

    errors = []
    for dep in sorted(manifest_deps):
        if dep not in all_imports:
            if (project_name, dep) in whitelist:
                continue
            errors.append(
                f"'{project_name}' declares dependency on '{dep}' "
                f"but no source file imports it"
            )

    return errors


def check_undeclared_deps(
    project_name: str,
    project_dir: str,
    manifest_deps: set[str],
    workspace_names: set[str],
    *,
    _cached_imports: tuple[set[str], set[str], set[str]] | None = None,
) -> list[str]:
    """Check for imports from workspace packages not declared as deps.

    Only checks lib/ imports (non-test context) against declared
    dependencies. Test files have more lenient rules and are skipped.
    Guarded imports (try/except ImportError) are excluded -- optional
    imports don't need to be declared as dependencies.

    Args:
        project_name: name of the project being checked.
        project_dir: absolute path to the project directory.
        manifest_deps: set of declared intra-workspace dependency names.
        workspace_names: set of all workspace member package names.
        _cached_imports: optional pre-computed (lib_imports, test_imports,
            guarded_imports) tuple to avoid redundant scans when multiple
            checks share the same project.

    Returns:
        list of error strings (empty means all good).
    """
    if _cached_imports is not None:
        lib_imports, _test_imports, _guarded_imports = _cached_imports
    else:
        lib_imports, _test_imports, _guarded_imports = _get_imported_workspace_packages(
            project_dir, workspace_names
        )

    errors = []
    for imported in sorted(lib_imports):
        if imported == project_name:
            # Self-imports are fine (e.g. package importing its own submodules)
            continue
        if imported not in manifest_deps:
            errors.append(
                f"'{project_name}' imports '{imported}' in lib code "
                f"but does not declare it as a dependency"
            )

    return errors


def check_runtime_test_only(
    manifest_deps_with_scope: dict[str, str],
    lib_imports: set[str],
    test_imports: set[str],
) -> list[str]:
    """Find runtime deps that are only used in test code.

    For each dependency where scope="runtime": if it appears in
    test_imports but NOT in lib_imports, it is flagged.

    Args:
        manifest_deps_with_scope: mapping of dep name -> scope string.
        lib_imports: workspace package names found in production code.
        test_imports: workspace package names found in test code.

    Returns:
        list of flagged dependency names.
    """
    flagged = []
    for dep_name, scope in sorted(manifest_deps_with_scope.items()):
        if scope != "runtime":
            continue
        if dep_name in test_imports and dep_name not in lib_imports:
            flagged.append(dep_name)
    return flagged


def check_dev_in_lib(
    manifest_deps_with_scope: dict[str, str],
    lib_imports: set[str],
) -> list[str]:
    """Find dev deps that are imported in production code.

    For each dependency where scope="dev": if it appears in
    lib_imports, it is flagged.

    Args:
        manifest_deps_with_scope: mapping of dep name -> scope string.
        lib_imports: workspace package names found in production code.

    Returns:
        list of flagged dependency names.
    """
    flagged = []
    for dep_name, scope in sorted(manifest_deps_with_scope.items()):
        if scope != "dev":
            continue
        if dep_name in lib_imports:
            flagged.append(dep_name)
    return flagged


def _is_non_production_path(filepath: str, project_dir: str) -> bool:
    """Check if a file path is in a non-production context.

    Delegates to _is_test_context from import_scanners.py to detect
    test directories, example directories, and test file patterns.
    """
    return _is_test_context(filepath, project_dir)


def _python_module_name(filepath: str, project_dir: str) -> str | None:
    """Derive the dotted module name from a Python file path.

    Returns None for files that cannot be mapped to a module name
    (e.g. files outside a package structure).
    """
    rel = os.path.relpath(filepath, project_dir)
    # Convert path separators to dots and strip .py extension
    if rel.endswith(".py"):
        rel = rel[:-3]
    else:
        return None
    parts = rel.split(os.sep)
    # Strip __init__ from the end (it represents the package itself)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _collect_python_imports(
    filepath: str, project_dir: str,
) -> set[str]:
    """Collect all import targets from a Python file.

    Returns a set of dotted module names that are imported.
    Handles absolute imports (import foo, from foo.bar import baz)
    and relative imports (from .utils import helper, from ..core import x).
    Relative imports are resolved to absolute dotted paths using the
    file's position within the project directory.
    """
    imports: set[str] = set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return imports

    # Determine this file's package for resolving relative imports
    rel = os.path.relpath(filepath, project_dir)
    parts = rel.split(os.sep)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    # For __init__.py, the module IS the package (don't strip last part)
    if parts[-1] == "__init__":
        file_package_parts = parts[:-1]
    else:
        file_package_parts = parts[:-1]

    # Match 'import x.y.z' and 'import x.y.z as alias'
    for m in re.finditer(r"^\s*import\s+([\w.]+)", content, re.MULTILINE):
        imports.add(m.group(1))

    # Match 'from x.y.z import ...' (absolute) and 'from .x import ...' (relative)
    for m in re.finditer(r"^\s*from\s+(\.+[\w.]*|[\w.]+)\s+import\s+", content, re.MULTILINE):
        module = m.group(1)
        if module.startswith("."):
            # Relative import: resolve to absolute path
            dots = len(module) - len(module.lstrip("."))
            relative_module = module.lstrip(".")
            # Go up 'dots - 1' levels from the current package
            if dots - 1 > len(file_package_parts):
                continue  # Can't go above project root
            base_parts = file_package_parts[:len(file_package_parts) - (dots - 1)]
            if relative_module:
                resolved = ".".join(base_parts + [relative_module]) if base_parts else relative_module
            else:
                resolved = ".".join(base_parts) if base_parts else ""
            if resolved:
                imports.add(resolved)
        else:
            imports.add(module)

    return imports


def _collect_init_exports(filepath: str) -> set[str]:
    """Collect names exported from an __init__.py file.

    Looks for __all__ definitions and import statements.
    Returns module names that are imported by the __init__.py.
    """
    exports: set[str] = set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return exports

    # Find __all__ = [...] and extract names
    all_match = re.search(
        r"__all__\s*=\s*\[([^\]]*)\]", content, re.DOTALL
    )
    if all_match:
        for name_match in re.finditer(r"""['"](\w+)['"]""", all_match.group(1)):
            exports.add(name_match.group(1))

    # Collect from .foo import bar (relative imports in __init__.py)
    for m in re.finditer(r"^\s*from\s+\.(\w+)\s+import\s+", content, re.MULTILINE):
        exports.add(m.group(1))

    # Collect import .foo (rare but possible)
    for m in re.finditer(r"^\s*from\s+\.\s+import\s+([\w,\s]+)", content, re.MULTILINE):
        for name in m.group(1).split(","):
            name = name.strip()
            if name:
                exports.add(name)

    return exports


def find_dead_modules(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[str]:
    """Find Python modules not referenced by any other module in the project.

    A module is considered dead if:
    1. No other module in the project imports it (by any prefix match)
    2. It is not listed in any __init__.py's __all__ or imported by
       any __init__.py

    Only checks Python projects. Non-production files (tests, examples)
    are excluded from the scan.

    Args:
        project_dir: absolute path to the project root.
        exclude_dirs: directory paths to skip during the walk
            (relative to project_dir or absolute).

    Returns:
        list of relative paths of dead modules (e.g. ["mylib/unused.py"]).
    """
    project_dir = os.path.abspath(project_dir)

    # Check this is a Python project
    if not os.path.isfile(os.path.join(project_dir, "pyproject.toml")):
        return []

    # Collect all .py files excluding non-production paths
    all_files = walk_source_files(project_dir, (".py",), [], exclude_dirs=exclude_dirs)
    production_files = [
        f for f in all_files
        if not _is_non_production_path(f, project_dir)
    ]

    if not production_files:
        return []

    # Build module name -> filepath mapping
    module_to_file: dict[str, str] = {}
    init_files: list[str] = []
    for filepath in production_files:
        if os.path.basename(filepath) == "__init__.py":
            init_files.append(filepath)
            continue
        mod_name = _python_module_name(filepath, project_dir)
        if mod_name:
            module_to_file[mod_name] = filepath

    if not module_to_file:
        return []

    # Collect all imports across all production files
    all_imports: set[str] = set()
    for filepath in production_files:
        all_imports.update(_collect_python_imports(filepath, project_dir))

    # Collect all __init__.py exports (names referenced via relative import
    # or __all__)
    init_exported_names: set[str] = set()
    for init_path in init_files:
        init_exported_names.update(_collect_init_exports(init_path))

    # Check each module for references
    dead = []
    for mod_name, filepath in sorted(module_to_file.items()):
        # Check if any import matches this module (prefix match)
        # e.g. module "foo.bar" is referenced by "import foo.bar" or
        # "from foo.bar import baz" or "import foo.bar.sub"
        is_referenced = False
        for imp in all_imports:
            # imp references mod_name if imp starts with mod_name
            # or mod_name starts with imp (importing a parent pulls in child)
            if imp == mod_name or imp.startswith(mod_name + ".") or mod_name.startswith(imp + "."):
                is_referenced = True
                break

        if is_referenced:
            continue

        # Check if the module's leaf name is exported by any __init__.py
        leaf_name = mod_name.rsplit(".", 1)[-1]
        if leaf_name in init_exported_names:
            continue

        rel_path = os.path.relpath(filepath, project_dir)
        dead.append(rel_path)

    return dead


def _read_go_module_path(project_dir: str) -> str | None:
    """Read the module path from go.mod.

    Returns None if go.mod does not exist or cannot be parsed.
    """
    go_mod = os.path.join(project_dir, "go.mod")
    if not os.path.isfile(go_mod):
        return None
    try:
        with open(go_mod, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[len("module "):].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _go_package_dir(filepath: str) -> str:
    """Return the directory of a Go file (its package directory)."""
    return os.path.dirname(filepath)


def find_dead_go_packages(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[str]:
    """Find Go internal packages not referenced by any non-test code.

    A Go internal package is dead if no non-test .go file outside that
    package imports it. Only packages under ``internal/`` subdirectories
    are checked, since those are the packages with restricted visibility
    in Go's module system.

    Args:
        project_dir: absolute path to the Go project root (where go.mod lives).
        exclude_dirs: directory paths to skip during the walk
            (relative to project_dir or absolute).

    Returns:
        list of relative paths of dead internal packages
        (e.g. ["internal/unused"]).
    """
    project_dir = os.path.abspath(project_dir)

    module_path = _read_go_module_path(project_dir)
    if module_path is None:
        return []

    # Find all .go files in the project
    all_go_files = walk_source_files(project_dir, (".go",), [], exclude_dirs=exclude_dirs)
    if not all_go_files:
        return []

    # Identify internal package directories and their import paths
    internal_pkg_dirs: dict[str, str] = {}  # abs_dir -> full_import_path
    for filepath in all_go_files:
        pkg_dir = _go_package_dir(filepath)
        if pkg_dir in internal_pkg_dirs:
            continue
        rel_dir = os.path.relpath(pkg_dir, project_dir)
        parts = rel_dir.split(os.sep)
        if "internal" in parts:
            import_path = module_path + "/" + rel_dir.replace(os.sep, "/")
            internal_pkg_dirs[pkg_dir] = import_path

    if not internal_pkg_dirs:
        return []

    # Collect imports per non-test .go file, keyed by the file's package dir
    # file_imports: list of (pkg_dir, set_of_import_paths) for non-test files
    file_imports: list[tuple[str, set[str]]] = []
    for filepath in all_go_files:
        if os.path.basename(filepath).endswith("_test.go"):
            continue
        pkg_dir_of_file = _go_package_dir(filepath)
        imports = {ip for ip, _fp, _ln in _go_scan_imports(filepath)}
        file_imports.append((pkg_dir_of_file, imports))

    # Check each internal package: is it imported by any non-test file
    # outside its own directory?
    dead = []
    for pkg_dir, import_path in sorted(internal_pkg_dirs.items(), key=lambda x: x[1]):
        is_referenced = False
        for file_pkg_dir, imports in file_imports:
            if file_pkg_dir == pkg_dir:
                continue  # same package -- doesn't count
            for imp in imports:
                if imp == import_path or imp.startswith(import_path + "/"):
                    is_referenced = True
                    break
            if is_referenced:
                break

        if not is_referenced:
            rel_path = os.path.relpath(pkg_dir, project_dir)
            dead.append(rel_path)

    return dead


# ---------------------------------------------------------------------------
# npm dead module detection (entry-point reachability)
# ---------------------------------------------------------------------------

# Extensions tried when resolving npm relative imports.
_NPM_RESOLVE_EXTENSIONS = (".ts", ".tsx", ".js", ".mjs", ".cjs")

# Index file names tried when a resolved path is a directory.
_NPM_INDEX_NAMES = (
    "index.ts", "index.tsx", "index.js", "index.mjs", "index.cjs",
)

# Source file extensions for npm projects.
_NPM_SOURCE_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs", ".tsx")


def _resolve_npm_file(path: str) -> str | None:
    """Resolve a single path to an existing file using npm conventions.

    Tries the exact path, then with each extension appended, then as
    a directory with index files. Also handles .js -> .ts mapping for
    TypeScript projects.

    Returns the absolute file path if found, None otherwise.
    """
    # Exact match
    if os.path.isfile(path):
        return path

    # Try appending extensions
    for ext in _NPM_RESOLVE_EXTENSIONS:
        candidate = path + ext
        if os.path.isfile(candidate):
            return candidate

    # .js -> .ts mapping: if path ends with .js but only .ts exists
    if path.endswith(".js"):
        ts_path = path[:-3] + ".ts"
        if os.path.isfile(ts_path):
            return ts_path
        tsx_path = path[:-3] + ".tsx"
        if os.path.isfile(tsx_path):
            return tsx_path

    # .jsx -> .tsx mapping
    if path.endswith(".jsx"):
        tsx_path = path[:-4] + ".tsx"
        if os.path.isfile(tsx_path):
            return tsx_path

    # Directory -> index file
    if os.path.isdir(path):
        for index_name in _NPM_INDEX_NAMES:
            candidate = os.path.join(path, index_name)
            if os.path.isfile(candidate):
                return candidate

    return None


def _collect_export_paths(value: object) -> list[str]:
    """Recursively collect all file path strings from a package.json exports value.

    The exports field can be:
    - A string: "./dist/index.js"
    - A dict with condition keys: {"import": "./dist/index.mjs", "require": "./dist/index.cjs"}
    - A nested subpath map: {".": {"import": "..."}, "./sub": "..."}
    - A list (rarely): ["./a.js", "./b.js"]

    Collects all string values that look like file paths (start with ".").
    """
    paths: list[str] = []
    if isinstance(value, str):
        if value.startswith("."):
            paths.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            paths.extend(_collect_export_paths(v))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_collect_export_paths(item))
    return paths


def _resolve_npm_entry_points(project_dir: str) -> set[str]:
    """Extract and resolve entry point file paths from package.json.

    Parses exports, main, and bin fields. Resolves each declared path
    to an absolute filesystem path, handling .js -> .ts mapping and
    directory -> index file resolution.

    Returns a set of absolute file paths. Missing files are skipped.
    """
    pkg_path = os.path.join(project_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return set()

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    raw_paths: list[str] = []

    # exports field
    exports = data.get("exports")
    if exports is not None:
        raw_paths.extend(_collect_export_paths(exports))

    # main field
    main = data.get("main")
    if isinstance(main, str):
        raw_paths.append(main)

    # bin field
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        raw_paths.append(bin_field)
    elif isinstance(bin_field, dict):
        for v in bin_field.values():
            if isinstance(v, str):
                raw_paths.append(v)

    # Resolve each path to an absolute file
    entry_points: set[str] = set()
    for raw in raw_paths:
        abs_path = os.path.normpath(os.path.join(project_dir, raw))
        resolved = _resolve_npm_file(abs_path)
        if resolved is not None:
            entry_points.add(os.path.abspath(resolved))

    return entry_points


def _build_npm_import_graph(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> dict[str, set[str]]:
    """Build a file-level import graph for an npm project.

    Uses NpmAstLinter.scan_imports() to collect all imports, then
    resolves relative imports to absolute file paths.

    Returns a dict mapping each source file's absolute path to a set
    of absolute paths it imports (only resolved relative imports).
    """
    from .lint.npm_ast import NpmAstLinter

    linter = NpmAstLinter()
    raw_imports = linter.scan_imports(project_dir, exclude_dirs=exclude_dirs)

    graph: dict[str, set[str]] = {}
    for specifier, filepath, _line in raw_imports:
        # Only process relative imports
        if not specifier.startswith("./") and not specifier.startswith("../"):
            continue

        abs_filepath = os.path.abspath(filepath)
        if abs_filepath not in graph:
            graph[abs_filepath] = set()

        # Resolve the import relative to the importing file's directory
        import_dir = os.path.dirname(abs_filepath)
        abs_target = os.path.normpath(os.path.join(import_dir, specifier))
        resolved = _resolve_npm_file(abs_target)
        if resolved is not None:
            graph[abs_filepath].add(os.path.abspath(resolved))

    return graph


def find_dead_npm_modules(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[str]:
    """Find npm source files unreachable from any package.json entry point.

    A source file is "dead" if there is no path through the import graph
    from any declared entry point (exports, main, bin) to that file.

    Args:
        project_dir: absolute path to the npm project root.
        exclude_dirs: directory paths to skip during the walk
            (relative to project_dir or absolute).

    Returns:
        sorted list of relative paths of dead source files.
    """
    project_dir = os.path.abspath(project_dir)

    if not os.path.isfile(os.path.join(project_dir, "package.json")):
        return []

    entry_points = _resolve_npm_entry_points(project_dir)
    if not entry_points:
        # No entry points declared -- cannot determine reachability.
        return []

    import_graph = _build_npm_import_graph(project_dir, exclude_dirs=exclude_dirs)

    # Collect all production source files
    all_files = walk_source_files(project_dir, _NPM_SOURCE_EXTENSIONS, [], exclude_dirs=exclude_dirs)
    production_files = {
        os.path.abspath(f)
        for f in all_files
        if not _is_non_production_path(f, project_dir)
    }

    if not production_files:
        return []

    # BFS from entry points
    reachable: set[str] = set()
    queue = list(entry_points & production_files)
    # Also seed with entry points that exist but may not be in
    # production_files (e.g. if entry point is outside src/)
    for ep in entry_points:
        if ep not in reachable:
            reachable.add(ep)
            queue.append(ep)

    while queue:
        current = queue.pop()
        for neighbor in import_graph.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)

    # Dead = production files not reachable
    dead = sorted(
        os.path.relpath(f, project_dir)
        for f in production_files
        if f not in reachable
    )
    return dead


# ---------------------------------------------------------------------------
# Dart dead module detection (entry-point reachability)
# ---------------------------------------------------------------------------

# Regex matching Dart import/export statements.
# Captures the quoted path from: import 'path'; / export 'path';
# Also handles double quotes and 'show'/'hide'/'as' suffixes.
_DART_IMPORT_EXPORT_RE = re.compile(
    r"""^\s*(?:import|export)\s+['"]([^'"]+)['"]"""
)


def _read_dart_package_name(project_dir: str) -> str | None:
    """Read the package name from pubspec.yaml.

    Returns None if pubspec.yaml does not exist or has no 'name' field.
    """
    pubspec_path = os.path.join(project_dir, "pubspec.yaml")
    if not os.path.isfile(pubspec_path):
        return None
    try:
        from ruamel.yaml import YAML
        yaml = YAML(typ="safe")
        with open(pubspec_path, "r", encoding="utf-8") as f:
            data = yaml.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("name")


def _resolve_dart_entry_points(project_dir: str) -> set[str]:
    """Determine Dart entry point files for reachability analysis.

    Entry points are:
    - lib/<package_name>.dart  (barrel file / main library entry)
    - bin/*.dart               (executable scripts)

    Returns a set of absolute file paths.
    """
    project_dir = os.path.abspath(project_dir)
    entry_points: set[str] = set()

    pkg_name = _read_dart_package_name(project_dir)
    if pkg_name:
        barrel = os.path.join(project_dir, "lib", f"{pkg_name}.dart")
        if os.path.isfile(barrel):
            entry_points.add(os.path.abspath(barrel))

    bin_dir = os.path.join(project_dir, "bin")
    if os.path.isdir(bin_dir):
        for name in os.listdir(bin_dir):
            if name.endswith(".dart"):
                entry_points.add(
                    os.path.abspath(os.path.join(bin_dir, name))
                )

    return entry_points


def _resolve_dart_import(
    specifier: str,
    importing_file: str,
    project_dir: str,
    package_name: str | None,
) -> str | None:
    """Resolve a Dart import specifier to an absolute file path.

    Handles:
    - Relative imports: 'src/foo.dart', '../utils.dart'
    - Self-package imports: 'package:mylib/src/foo.dart' -> lib/src/foo.dart

    Returns absolute path if resolved, None otherwise.
    Skips dart: imports and external package: imports.
    """
    if specifier.startswith("dart:"):
        return None

    # Self-package import: package:<name>/path
    if specifier.startswith("package:"):
        without_prefix = specifier[len("package:"):]
        slash_idx = without_prefix.find("/")
        if slash_idx < 0:
            return None
        pkg = without_prefix[:slash_idx]
        rest = without_prefix[slash_idx + 1:]
        if pkg != package_name:
            # External package -- not part of intra-package graph
            return None
        # Self-package: resolve to lib/<rest>
        resolved = os.path.normpath(os.path.join(project_dir, "lib", rest))
        if os.path.isfile(resolved):
            return os.path.abspath(resolved)
        return None

    # Relative import: resolve relative to the importing file's directory
    import_dir = os.path.dirname(importing_file)
    resolved = os.path.normpath(os.path.join(import_dir, specifier))
    if os.path.isfile(resolved):
        return os.path.abspath(resolved)
    return None


def _build_dart_import_graph(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> dict[str, set[str]]:
    """Build a file-level import graph for a Dart project.

    Walks all .dart files, extracts import/export statements via regex,
    and resolves relative and self-package imports to absolute paths.

    Returns a dict mapping each source file's absolute path to a set
    of absolute paths it imports/exports (only resolved intra-package refs).
    """
    project_dir = os.path.abspath(project_dir)
    package_name = _read_dart_package_name(project_dir)

    dart_files = walk_source_files(
        project_dir, (".dart",), [], exclude_dirs=exclude_dirs,
    )

    graph: dict[str, set[str]] = {}
    for filepath in dart_files:
        abs_filepath = os.path.abspath(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue

        deps: set[str] = set()
        for line in content.splitlines():
            m = _DART_IMPORT_EXPORT_RE.match(line)
            if not m:
                continue
            specifier = m.group(1)
            resolved = _resolve_dart_import(
                specifier, abs_filepath, project_dir, package_name,
            )
            if resolved is not None:
                deps.add(resolved)

        if deps:
            graph[abs_filepath] = deps

    return graph


def find_dead_dart_modules(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[str]:
    """Find Dart source files unreachable from any entry point.

    A .dart file is "dead" if there is no path through the import/export
    graph from any entry point (barrel file, bin scripts) to that file.

    Test files (test/, *_test.dart) are excluded from the scan.

    Args:
        project_dir: absolute path to the Dart project root.
        exclude_dirs: directory paths to skip during the walk
            (relative to project_dir or absolute).

    Returns:
        sorted list of relative paths of dead source files.
    """
    project_dir = os.path.abspath(project_dir)

    if not os.path.isfile(os.path.join(project_dir, "pubspec.yaml")):
        return []

    entry_points = _resolve_dart_entry_points(project_dir)
    if not entry_points:
        return []

    import_graph = _build_dart_import_graph(
        project_dir, exclude_dirs=exclude_dirs,
    )

    # Collect all production .dart files
    all_files = walk_source_files(
        project_dir, (".dart",), [], exclude_dirs=exclude_dirs,
    )
    production_files = {
        os.path.abspath(f)
        for f in all_files
        if not _is_non_production_path(f, project_dir)
    }

    if not production_files:
        return []

    # BFS from entry points
    reachable: set[str] = set()
    queue: deque[str] = deque()

    for ep in entry_points:
        if ep not in reachable:
            reachable.add(ep)
            queue.append(ep)

    while queue:
        current = queue.popleft()
        for neighbor in import_graph.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)

    # Dead = production files not reachable from any entry point
    dead = sorted(
        os.path.relpath(f, project_dir)
        for f in production_files
        if f not in reachable
    )
    return dead


# ---------------------------------------------------------------------------
# Workspace-level dead export detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeadWorkspacePackage:
    """A workspace package with no workspace importers."""

    name: str
    severity: str  # "error" or "warn"
    message: str


def find_dead_workspace_packages(
    projects: list[dict],
    import_cache: dict[str, tuple[set[str], set[str]]],
) -> list[DeadWorkspacePackage]:
    """Find library packages that no other workspace package imports.

    A library package is "dead" at the workspace level if its name does
    not appear in any other project's lib_imports or test_imports sets.

    Args:
        projects: list of workspace project dicts (must have "name",
            and optionally "library" and "dev_node" keys).
        import_cache: mapping of project name to (lib_imports, test_imports)
            as produced by _build_dep_import_cache in checks.py.

    Returns:
        list of DeadWorkspacePackage for packages with no workspace importers.
    """
    # Build reverse-import map: for each package name, which projects
    # import it (split by lib vs test context).
    lib_importers: dict[str, set[str]] = {}
    test_importers: dict[str, set[str]] = {}

    for proj in projects:
        proj_name = proj["name"]
        lib_imports, test_imports = import_cache.get(proj_name, (set(), set()))
        for imported in lib_imports:
            if imported == proj_name:
                continue  # self-imports don't count
            lib_importers.setdefault(imported, set()).add(proj_name)
        for imported in test_imports:
            if imported == proj_name:
                continue
            test_importers.setdefault(imported, set()).add(proj_name)

    results: list[DeadWorkspacePackage] = []

    for proj in projects:
        name = proj["name"]

        # Skip dev_node projects -- excluded from most checks
        if proj.get("dev_node"):
            continue

        # Skip non-library projects (apps, CLIs) -- they are entry points
        # that consume but aren't consumed.
        if not proj.get("library"):
            continue

        has_lib_importers = bool(lib_importers.get(name))
        has_test_importers = bool(test_importers.get(name))

        if has_lib_importers:
            # Imported in production code by at least one sibling -- alive
            continue

        if has_test_importers and not has_lib_importers:
            # Only imported in tests by workspace siblings
            importers = sorted(test_importers[name])
            results.append(DeadWorkspacePackage(
                name=name,
                severity="warn",
                message=(
                    f"library '{name}' is only imported in test code by "
                    f"workspace siblings ({', '.join(importers)})"
                ),
            ))
            continue

        # Zero workspace importers. Published libraries might still be
        # consumed externally, so this is a warning not an error.
        results.append(DeadWorkspacePackage(
            name=name,
            severity="warn",
            message=(
                f"library '{name}' is not imported by any workspace package"
            ),
        ))

    return results


# ---------------------------------------------------------------------------
# Circular dependency detection (Tarjan's SCC)
# ---------------------------------------------------------------------------


def find_circular_deps(import_graph: dict[str, set[str]]) -> list[list[str]]:
    """Find circular dependencies in a file-level import graph using Tarjan's SCC.

    Args:
        import_graph: mapping of file path to the set of file paths it imports.

    Returns:
        list of cycles, where each cycle is a list of file paths forming
        the strongly connected component. Only SCCs with 2+ nodes are
        returned (self-loops are not interesting).
    """
    # Tarjan's strongly connected components algorithm
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    # Collect all nodes (some may only appear as targets, not keys)
    all_nodes: set[str] = set(import_graph.keys())
    for targets in import_graph.values():
        all_nodes.update(targets)

    def strongconnect(node: str) -> None:
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in import_graph.get(node, set()):
            if neighbor not in index:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], index[neighbor])

        # Root of an SCC
        if lowlink[node] == index[node]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == node:
                    break
            if len(component) >= 2:
                result.append(sorted(component))

    for node in sorted(all_nodes):
        if node not in index:
            strongconnect(node)

    return result


def _build_python_import_graph(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> dict[str, set[str]]:
    """Build a file-level import graph for a Python project.

    Uses _collect_python_imports to get dotted module names, then resolves
    them to file paths via a module-name-to-file mapping.

    Returns a dict mapping each source file's relative path to a set of
    relative paths it imports (only intra-project imports that resolve to
    actual files).
    """
    project_dir = os.path.abspath(project_dir)

    if not os.path.isfile(os.path.join(project_dir, "pyproject.toml")):
        return {}

    all_files = walk_source_files(project_dir, (".py",), [], exclude_dirs=exclude_dirs)
    production_files = [
        f for f in all_files
        if not _is_non_production_path(f, project_dir)
    ]

    if not production_files:
        return {}

    # Build module name -> relative path mapping
    module_to_relpath: dict[str, str] = {}
    for filepath in production_files:
        mod_name = _python_module_name(filepath, project_dir)
        if mod_name:
            module_to_relpath[mod_name] = os.path.relpath(filepath, project_dir)

    # Build file-level import graph using relative paths
    graph: dict[str, set[str]] = {}
    for filepath in production_files:
        rel_path = os.path.relpath(filepath, project_dir)
        imports = _collect_python_imports(filepath, project_dir)
        resolved: set[str] = set()
        for imp in imports:
            # Try exact match first, then prefix match for sub-modules
            if imp in module_to_relpath:
                target = module_to_relpath[imp]
                if target != rel_path:
                    resolved.add(target)
            else:
                # Check if any module starts with this import (parent import)
                for mod_name, mod_path in module_to_relpath.items():
                    if mod_name.startswith(imp + ".") and mod_path != rel_path:
                        resolved.add(mod_path)
        if resolved:
            graph[rel_path] = resolved

    return graph


def find_circular_python_deps(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[list[str]]:
    """Find circular dependencies in a Python project.

    Builds a file-level import graph from Python source files and runs
    Tarjan's SCC algorithm to detect cycles.

    Args:
        project_dir: absolute path to the project root.
        exclude_dirs: directory paths to skip during the walk.

    Returns:
        list of cycles, each a sorted list of relative file paths.
    """
    graph = _build_python_import_graph(project_dir, exclude_dirs=exclude_dirs)
    return find_circular_deps(graph)


def find_circular_npm_deps(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[list[str]]:
    """Find circular dependencies in an npm project.

    Reuses _build_npm_import_graph() and runs Tarjan's SCC algorithm.

    Args:
        project_dir: absolute path to the project root.
        exclude_dirs: directory paths to skip during the walk.

    Returns:
        list of cycles, each a sorted list of relative file paths.
    """
    project_dir = os.path.abspath(project_dir)

    if not os.path.isfile(os.path.join(project_dir, "package.json")):
        return []

    abs_graph = _build_npm_import_graph(project_dir, exclude_dirs=exclude_dirs)

    # Convert absolute paths to relative for consistent output
    rel_graph: dict[str, set[str]] = {}
    for src, targets in abs_graph.items():
        rel_src = os.path.relpath(src, project_dir)
        rel_graph[rel_src] = {os.path.relpath(t, project_dir) for t in targets}

    return find_circular_deps(rel_graph)


def find_circular_dart_deps(
    project_dir: str,
    exclude_dirs: list[str] | None = None,
) -> list[list[str]]:
    """Find circular dependencies in a Dart project.

    Builds a file-level import graph from Dart source files using regex
    to extract relative imports, then runs Tarjan's SCC algorithm.

    Args:
        project_dir: absolute path to the project root.
        exclude_dirs: directory paths to skip during the walk.

    Returns:
        list of cycles, each a sorted list of relative file paths.
    """
    project_dir = os.path.abspath(project_dir)

    pubspec = os.path.join(project_dir, "pubspec.yaml")
    if not os.path.isfile(pubspec):
        return []

    dart_files = walk_source_files(project_dir, (".dart",), [], exclude_dirs=exclude_dirs)
    production_files = [
        f for f in dart_files
        if not _is_non_production_path(f, project_dir)
    ]

    if not production_files:
        return []

    # Build file-level import graph from relative Dart imports
    _dart_relative_import_re = re.compile(
        r"""(?:import|export)\s+['"](?!package:|dart:)([\w./]+)['"]"""
    )

    graph: dict[str, set[str]] = {}
    for filepath in production_files:
        rel_path = os.path.relpath(filepath, project_dir)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue

        targets: set[str] = set()
        for match in _dart_relative_import_re.finditer(content):
            import_path = match.group(1)
            # Resolve relative to the importing file's directory
            import_dir = os.path.dirname(filepath)
            abs_target = os.path.normpath(os.path.join(import_dir, import_path))
            if os.path.isfile(abs_target):
                target_rel = os.path.relpath(abs_target, project_dir)
                if target_rel != rel_path:
                    targets.add(target_rel)

        if targets:
            graph[rel_path] = targets

    return find_circular_deps(graph)
