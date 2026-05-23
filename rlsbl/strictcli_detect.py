"""Detect whether a project uses strictcli and extract its entry point."""

import os

import tomlkit


def detect_strictcli(project_dir: str = ".") -> tuple[str, str] | None:
    """Check if a project uses strictcli and return its entry point info.

    Reads pyproject.toml from project_dir. If the project depends on strictcli
    and has a [project.scripts] entry, returns (entry_point_name, "python").
    Returns None if the project does not use strictcli.

    Args:
        project_dir: path to the project root (default: current directory).

    Returns:
        A tuple (entry_point_name, "python") if strictcli is detected, else None.
    """
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
