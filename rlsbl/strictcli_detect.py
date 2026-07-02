"""Detect whether a project uses strictcli and extract its entry point."""

import glob
import os

import tomlkit

from .errors import RlsblError


_STRICTCLI_GO_MODULE = "github.com/smm-h/strictcli"


class StrictcliDetectError(RlsblError):
    """The project requires strictcli but its entry point cannot be
    determined -- callers must treat this as a hard error, never as
    'project does not use strictcli'."""


def detect_strictcli(project_dir: str = ".") -> tuple[str, str] | None:
    """Check if a project uses strictcli and return its entry point info.

    First checks Python (pyproject.toml). If not found, checks Go (go.mod).

    For Python: if the project depends on strictcli and has a [project.scripts]
    entry, returns (entry_point_name, "python").

    For Go: if go.mod requires github.com/smm-h/strictcli, enumerates main
    packages via the go toolchain (go_introspect) and returns
    (package_path, "go") -- see _detect_go_strictcli for the resolution
    rules (single main, or the one main whose dir imports strictcli).

    Args:
        project_dir: path to the project root (default: current directory).

    Returns:
        A tuple (entry_point, language) if strictcli is detected, else None.
    """
    result = _detect_python_strictcli(project_dir)
    if result:
        return result
    return _detect_go_strictcli(project_dir)


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


def _is_strictcli_module_path(path: str) -> bool:
    """Return True if a Go module path is strictcli or a sub-path of it
    (e.g. github.com/smm-h/strictcli/go), not a mere string prefix
    (e.g. github.com/smm-h/strictcli-extras)."""
    return path == _STRICTCLI_GO_MODULE or path.startswith(
        _STRICTCLI_GO_MODULE + "/"
    )


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
    """
    from .lint.go_ast import scan_imports

    for import_path, _, _ in scan_imports(filepath):
        if import_path.startswith(_STRICTCLI_GO_MODULE):
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
