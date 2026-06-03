"""Dependency validation for monorepo workspaces.

Checks for unused declared dependencies and undeclared imports
across workspace projects. Uses import scanners to compare actual
source imports against manifest-declared dependencies.
"""

import os
import re
import tomllib

from .import_scanners import (
    DartImportScanner,
    NpmImportScanner,
    PythonImportScanner,
    _NON_PRODUCTION_PATTERNS,
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
            raise ValueError(
                f"unused_allowed[{i}] must be a table"
            )
        for key in ("package", "dep", "reason"):
            if key not in entry:
                raise ValueError(
                    f"unused_allowed[{i}] missing required key '{key}'"
                )
            if not isinstance(entry[key], str):
                raise ValueError(
                    f"unused_allowed[{i}].{key} must be a string"
                )
        if not entry["reason"].strip():
            raise ValueError(
                f"unused_allowed[{i}].reason must not be empty"
            )
        result[(entry["package"], entry["dep"])] = entry["reason"]

    return result


def _get_imported_workspace_packages(
    project_dir: str,
    workspace_names: set[str],
) -> tuple[set[str], set[str]]:
    """Scan a project for workspace imports, split by context.

    Returns (lib_imports, test_imports) where each is a set of
    workspace package names found in lib/test contexts respectively.
    """
    lib_imports: set[str] = set()
    test_imports: set[str] = set()

    for scanner in (PythonImportScanner(), DartImportScanner(), NpmImportScanner()):
        try:
            results = scanner.scan(project_dir, workspace_names)
        except RuntimeError:
            # DartImportScanner raises RuntimeError for missing .g.dart;
            # skip gracefully for dep validation purposes.
            continue
        for info in results:
            if info.is_test_context:
                test_imports.add(info.package_name)
            else:
                lib_imports.add(info.package_name)

    return lib_imports, test_imports


def check_unused_deps(
    project_name: str,
    project_dir: str,
    manifest_deps: set[str],
    workspace_names: set[str],
    whitelist: dict[tuple[str, str], str],
    *,
    _cached_imports: tuple[set[str], set[str]] | None = None,
) -> list[str]:
    """Check for declared workspace deps that no source file imports.

    Args:
        project_name: name of the project being checked.
        project_dir: absolute path to the project directory.
        manifest_deps: set of declared intra-workspace dependency names.
        workspace_names: set of all workspace member package names.
        whitelist: mapping of (package, dep) -> reason for allowed unused deps.
        _cached_imports: optional pre-computed (lib_imports, test_imports) tuple
            to avoid redundant scans when multiple checks share the same project.

    Returns:
        list of error strings (empty means all good).
    """
    if not manifest_deps:
        return []

    if _cached_imports is not None:
        lib_imports, test_imports = _cached_imports
    else:
        lib_imports, test_imports = _get_imported_workspace_packages(
            project_dir, workspace_names
        )
    all_imports = lib_imports | test_imports

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
    _cached_imports: tuple[set[str], set[str]] | None = None,
) -> list[str]:
    """Check for imports from workspace packages not declared as deps.

    Only checks lib/ imports (non-test context) against declared
    dependencies. Test files have more lenient rules and are skipped.

    Args:
        project_name: name of the project being checked.
        project_dir: absolute path to the project directory.
        manifest_deps: set of declared intra-workspace dependency names.
        workspace_names: set of all workspace member package names.
        _cached_imports: optional pre-computed (lib_imports, test_imports) tuple
            to avoid redundant scans when multiple checks share the same project.

    Returns:
        list of error strings (empty means all good).
    """
    if _cached_imports is not None:
        lib_imports, _test_imports = _cached_imports
    else:
        lib_imports, _test_imports = _get_imported_workspace_packages(
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

    Uses _NON_PRODUCTION_PATTERNS from import_scanners.py to detect
    test directories, example directories, and test file patterns.
    """
    rel = os.path.relpath(filepath, project_dir)
    parts = rel.split(os.sep)
    non_prod_dirs = (
        _NON_PRODUCTION_PATTERNS["test_dirs"]
        | _NON_PRODUCTION_PATTERNS["example_dirs"]
    )
    if any(part in non_prod_dirs for part in parts):
        return True
    basename = parts[-1]
    return any(
        pat.match(basename)
        for pat in _NON_PRODUCTION_PATTERNS["test_file_patterns"]
    )


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


def find_dead_modules(project_dir: str) -> list[str]:
    """Find Python modules not referenced by any other module in the project.

    A module is considered dead if:
    1. No other module in the project imports it (by any prefix match)
    2. It is not listed in any __init__.py's __all__ or imported by
       any __init__.py

    Only checks Python projects. Non-production files (tests, examples)
    are excluded from the scan.

    Args:
        project_dir: absolute path to the project root.

    Returns:
        list of relative paths of dead modules (e.g. ["mylib/unused.py"]).
    """
    project_dir = os.path.abspath(project_dir)

    # Check this is a Python project
    if not os.path.isfile(os.path.join(project_dir, "pyproject.toml")):
        return []

    # Collect all .py files excluding non-production paths
    all_files = walk_source_files(project_dir, (".py",), [])
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


def find_dead_go_packages(project_dir: str) -> list[str]:
    """Find Go internal packages not referenced by any non-test code.

    A Go internal package is dead if no non-test .go file outside that
    package imports it. Only packages under ``internal/`` subdirectories
    are checked, since those are the packages with restricted visibility
    in Go's module system.

    Args:
        project_dir: absolute path to the Go project root (where go.mod lives).

    Returns:
        list of relative paths of dead internal packages
        (e.g. ["internal/unused"]).
    """
    project_dir = os.path.abspath(project_dir)

    module_path = _read_go_module_path(project_dir)
    if module_path is None:
        return []

    # Find all .go files in the project
    all_go_files = walk_source_files(project_dir, (".go",), [])
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
