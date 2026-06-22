"""Detect whether a project uses strictcli and extract its entry point."""

import glob
import os
import re

import tomlkit


_STRICTCLI_GO_MODULE = "github.com/smm-h/strictcli"


def detect_strictcli(project_dir: str = ".") -> tuple[str, str] | None:
    """Check if a project uses strictcli and return its entry point info.

    First checks Python (pyproject.toml). If not found, checks Go (go.mod).

    For Python: if the project depends on strictcli and has a [project.scripts]
    entry, returns (entry_point_name, "python").

    For Go: if go.mod requires github.com/smm-h/strictcli, detects the entry
    point (root main.go or cmd/*/main.go) and returns (package_path, "go").

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


def _go_mod_has_strictcli(project_dir: str) -> bool:
    """Return True if go.mod requires a strictcli module."""
    go_mod_path = os.path.join(project_dir, "go.mod")
    if not os.path.exists(go_mod_path):
        return False
    with open(go_mod_path, "r", encoding="utf-8") as f:
        content = f.read()
    # Match require lines like: github.com/smm-h/strictcli/go v0.9.0
    # The module path may be strictcli itself or a sub-path like strictcli/go
    return bool(re.search(
        r"(?:^|\s)" + re.escape(_STRICTCLI_GO_MODULE) + r"(?:/\S*)?\s",
        content,
        re.MULTILINE,
    ))


def _go_file_imports_strictcli(filepath: str) -> bool:
    """Return True if a Go source file imports a strictcli package.

    Uses tree-sitter via lint/go_ast.scan_imports for accurate parsing.
    """
    from .lint.go_ast import scan_imports

    for import_path, _, _ in scan_imports(filepath):
        if import_path.startswith(_STRICTCLI_GO_MODULE):
            return True
    return False


def _detect_go_strictcli(project_dir: str) -> tuple[str, str] | None:
    """Detect strictcli in a Go project via go.mod.

    Detection order:
    1. Root main.go that imports strictcli -> return (".", "go")
    2. Single cmd/*/main.go -> return ("./cmd/<name>/", "go")
    3. Multiple cmd/*/main.go -> scan each for strictcli imports,
       return the one that imports it
    """
    if not _go_mod_has_strictcli(project_dir):
        return None

    # Check root main.go
    root_main = os.path.join(project_dir, "main.go")
    if os.path.exists(root_main):
        if _go_file_imports_strictcli(root_main):
            return (".", "go")

    # Check cmd/*/main.go entries
    cmd_mains = glob.glob(os.path.join(project_dir, "cmd", "*", "main.go"))
    if not cmd_mains:
        return None

    if len(cmd_mains) == 1:
        cmd_name = os.path.basename(os.path.dirname(cmd_mains[0]))
        return (f"./cmd/{cmd_name}/", "go")

    # Multi-binary: scan each for direct strictcli imports (lazy import)
    for main_go in cmd_mains:
        if _go_file_imports_strictcli(main_go):
            cmd_name = os.path.basename(os.path.dirname(main_go))
            return (f"./cmd/{cmd_name}/", "go")

    return None
