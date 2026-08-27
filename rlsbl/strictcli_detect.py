"""Detect whether a project uses strictcli and extract its entry point."""

import glob
import json
import os

import tomlkit

from .errors import RlsblError
from .module_paths import go_import_under_module


_STRICTCLI_GO_MODULE = "github.com/smm-h/strictcli"


class StrictcliDetectError(RlsblError):
    """The project requires strictcli but its entry point cannot be
    determined -- callers must treat this as a hard error, never as
    'project does not use strictcli'."""


def detect_strictcli(project_dir: str = ".") -> tuple[str, str] | None:
    """Check if a project uses strictcli and return its entry point info.

    Checks Python (pyproject.toml), then Go (go.mod), then TypeScript
    (package.json) -- one branch per strictcli implementation.

    For Python: if the project depends on strictcli and has a [project.scripts]
    entry, returns (entry_point_name, "python").

    For Go: if go.mod requires github.com/smm-h/strictcli, enumerates main
    packages via the go toolchain (go_introspect) and returns
    (package_path, "go") -- see _detect_go_strictcli for the resolution
    rules (single main, or the one main whose dir imports strictcli).

    For TypeScript: if package.json depends on the ``strictcli`` npm package
    and declares a ``bin``, returns (bin_script_path, "typescript").

    Args:
        project_dir: path to the project root (default: current directory).

    Returns:
        A tuple (entry_point, language) if strictcli is detected, else None.
    """
    result = _detect_python_strictcli(project_dir)
    if result:
        return result
    result = _detect_go_strictcli(project_dir)
    if result:
        return result
    return _detect_ts_strictcli(project_dir)


def _detect_python_strictcli(project_dir: str) -> tuple[str, str] | None:
    """Detect strictcli in a Python project via pyproject.toml."""
    pyproject_path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.exists(pyproject_path):
        return None

    with open(pyproject_path, "r", encoding="utf-8") as f:
        data = tomlkit.load(f)

    project = data.get("project")
    if not project:
        return None

    # Check if strictcli is in dependencies
    deps = project.get("dependencies", [])
    has_strictcli = any(
        dep.strip().startswith("strictcli") for dep in deps
    )
    if not has_strictcli:
        return None

    # Extract the first entry point from [project.scripts]
    scripts = project.get("scripts")
    if not scripts:
        return None

    # Get the first key (entry point name)
    entry_point = next(iter(scripts))
    return (entry_point, "python")


_TS_DEP_SECTIONS = ("dependencies", "devDependencies", "peerDependencies")


def _detect_ts_strictcli(project_dir: str) -> tuple[str, str] | None:
    """Detect strictcli in a TypeScript/npm project via package.json.

    A strictcli TS app depends on the ``strictcli`` npm package and declares a
    ``bin``. The entry point is the bin's SCRIPT PATH (not its command name):
    the dump runs it with node directly, so it does not depend on the package
    having been installed or linked first.

    ``bin`` takes two shapes -- a bare string (the package's own name is the
    command) or a map of command name to path. A map with several commands is
    resolved the way the Go branch resolves several main packages: it is not
    resolvable, so it is a hard error rather than a silent "no strictcli here".
    """
    package_json_path = os.path.join(project_dir, "package.json")
    if not os.path.exists(package_json_path):
        return None

    try:
        with open(package_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    has_strictcli = any(
        "strictcli" in (data.get(section) or {}) for section in _TS_DEP_SECTIONS
    )
    if not has_strictcli:
        return None

    bin_entry = data.get("bin")
    if isinstance(bin_entry, str):
        return (bin_entry, "typescript")
    if isinstance(bin_entry, dict) and len(bin_entry) == 1:
        return (next(iter(bin_entry.values())), "typescript")

    declared = ", ".join(f'"{k}"' for k in (bin_entry or {})) or "(none)"
    raise StrictcliDetectError(
        f"package.json in {project_dir} depends on strictcli, but the entry "
        f"point could not be determined: bin entries declared: {declared}. "
        "Declare exactly one bin so the schema dump knows what to run."
    )


def _is_strictcli_module_path(path: str) -> bool:
    """Return True if a Go module path is strictcli or a sub-path of it
    (e.g. github.com/smm-h/strictcli/go), not a mere string prefix
    (e.g. github.com/smm-h/strictcli-extras)."""
    return go_import_under_module(path, _STRICTCLI_GO_MODULE)


def _go_mod_has_strictcli(project_dir: str) -> bool:
    """Return True if go.mod has a require directive for a strictcli module.

    Only require directives count -- both the single-line form
    (`require path version`) and the block form (`require ( ... )`).
    The `module` declaration is never a match: the strictcli library's
    own go.mod declares `module github.com/smm-h/strictcli/go`, and
    treating that as a dependency made rlsbl demand a strictcli entry
    point from the strictcli library itself.
    """
    go_mod_path = os.path.join(project_dir, "go.mod")
    if not os.path.exists(go_mod_path):
        return False
    with open(go_mod_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    in_require_block = False
    for raw_line in lines:
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if in_require_block:
            if line == ")":
                in_require_block = False
                continue
            module_path = line.split()[0]
            if _is_strictcli_module_path(module_path):
                return True
            continue
        if line == "require (":
            in_require_block = True
            continue
        if line.startswith("require "):
            rest = line[len("require "):].strip()
            if rest == "(":
                in_require_block = True
                continue
            module_path = rest.split()[0]
            if _is_strictcli_module_path(module_path):
                return True
    return False


def _go_file_imports_strictcli(filepath: str) -> bool:
    """Return True if a Go source file imports a strictcli package.

    Uses tree-sitter via lint/go_ast.scan_imports for accurate parsing.

    The containment question is the shared one (:mod:`rlsbl.module_paths`),
    not a bare ``startswith``: this used to read an import of an unrelated
    ``github.com/smm-h/strictcli-extras/...`` as an import of strictcli, and
    this function is the tie-breaker that picks a repo's CLI entry point when
    several main packages exist.
    """
    from .lint.go_ast import scan_imports

    for import_path, _, _ in scan_imports(filepath):
        if _is_strictcli_module_path(import_path):
            return True
    return False


def _go_package_imports_strictcli(project_dir: str, rel_dir: str) -> bool:
    """Return True if any .go file in the package dir imports strictcli."""
    pkg_dir = os.path.normpath(os.path.join(project_dir, rel_dir))
    for go_file in glob.glob(os.path.join(pkg_dir, "*.go")):
        if _go_file_imports_strictcli(go_file):
            return True
    return False


def _entry_point_path(rel_dir: str) -> str:
    """Format a main-package rel_dir as a `go run` entry point path."""
    return "." if rel_dir == "." else rel_dir + "/"


def _detect_go_strictcli(project_dir: str) -> tuple[str, str] | None:
    """Detect strictcli in a Go project via go.mod and go list.

    Main packages are enumerated with the go toolchain (go_introspect),
    which handles entry files not named main.go, _test.go files in
    package main, and broken-root layouts.

    Resolution:
    1. Exactly one main package -> that's the entry point (the strictcli
       import may be indirect via an internal package).
    2. Multiple main packages -> the one whose dir directly imports
       strictcli.
    3. Anything else -> StrictcliDetectError: go.mod requires strictcli,
       so failing to find the entry point is a hard error, never a
       silent 'no strictcli here'.
    """
    if not _go_mod_has_strictcli(project_dir):
        return None

    from .go_introspect import list_main_packages

    mains = list_main_packages(project_dir)
    if len(mains) == 1:
        return (_entry_point_path(mains[0].rel_dir), "go")

    importing = [
        p for p in mains
        if _go_package_imports_strictcli(project_dir, p.rel_dir)
    ]
    if len(importing) == 1:
        return (_entry_point_path(importing[0].rel_dir), "go")

    detected = ", ".join(f'"{p.rel_dir}"' for p in mains) or "(none)"
    raise StrictcliDetectError(
        f"go.mod in {project_dir} requires strictcli, but the entry point "
        f"could not be determined: main packages detected: {detected}; "
        f"packages directly importing strictcli: {len(importing)}. "
        "Fix the project layout so exactly one main package uses strictcli."
    )
