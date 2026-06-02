"""Dependency validation for monorepo workspaces.

Checks for unused declared dependencies and undeclared imports
across workspace projects. Uses import scanners to compare actual
source imports against manifest-declared dependencies.
"""

import os
import tomllib

from .import_scanners import DartImportScanner, NpmImportScanner, PythonImportScanner
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
