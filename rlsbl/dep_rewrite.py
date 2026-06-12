"""Path dependency detection and rewriting for PyPI packages, converting local file references in pyproject.toml to versioned constraints."""

import os
import sys

import tomlkit

from .errors import VersionError
from .targets import TARGETS, detect_targets
from .workspace_graph import _parse_pypi_dep_name


def detect_path_deps(pyproject_path):
    """Detect path dependencies in a pyproject.toml file.

    Returns a list of dicts with keys:
      - name: the dependency package name
      - original: the full original dependency string
      - line_in_deps: index within the dependencies array
      - section: "dependencies" or "optional-dependencies.<group>"
    """
    if not os.path.isfile(pyproject_path):
        return []

    try:
        with open(pyproject_path, "r", encoding="utf-8") as f:
            data = tomlkit.parse(f.read())
    except Exception as exc:
        print(f"Warning: failed to parse {pyproject_path}: {exc}", file=sys.stderr)
        return []

    project = data.get("project", {})
    results = []

    # Main dependencies
    main_deps = project.get("dependencies", [])
    for i, dep_str in enumerate(main_deps):
        name, is_path, _constraint = _parse_pypi_dep_name(str(dep_str))
        if name and is_path:
            results.append({
                "name": name,
                "original": str(dep_str),
                "line_in_deps": i,
                "section": "dependencies",
            })

    # Optional dependencies
    optional = project.get("optional-dependencies", {})
    for group_name, group_deps in optional.items():
        for i, dep_str in enumerate(group_deps):
            name, is_path, _constraint = _parse_pypi_dep_name(str(dep_str))
            if name and is_path:
                results.append({
                    "name": name,
                    "original": str(dep_str),
                    "line_in_deps": i,
                    "section": f"optional-dependencies.{group_name}",
                })

    return results


def rewrite_pyproject_deps(content, rewrites):
    """Rewrite path dependencies in pyproject.toml content to versioned constraints.

    Args:
        content: raw pyproject.toml content string
        rewrites: dict mapping package name to version constraint string
                  (e.g., {"core": ">=1.2.0"})

    Returns the modified content as a string with formatting preserved.
    """
    if not rewrites:
        return content

    doc = tomlkit.parse(content)
    project = doc.get("project")
    if project is None:
        return content

    # Rewrite main dependencies
    main_deps = project.get("dependencies")
    if main_deps is not None:
        _rewrite_dep_array(main_deps, rewrites)

    # Rewrite optional dependencies
    optional = project.get("optional-dependencies")
    if optional is not None:
        for group_deps in optional.values():
            _rewrite_dep_array(group_deps, rewrites)

    return tomlkit.dumps(doc)


def _rewrite_dep_array(dep_array, rewrites):
    """Rewrite path deps in a tomlkit dependency array in place."""
    for i in range(len(dep_array)):
        dep_str = str(dep_array[i])
        name, is_path, _constraint = _parse_pypi_dep_name(dep_str)
        if name and is_path and name in rewrites:
            dep_array[i] = f"{name}{rewrites[name]}"


def build_rewrite_map(workspace_root, projects, graph):
    """Build a mapping of dependency names to version constraints.

    For each project in the workspace that has a detectable version,
    adds ``name: ">=version"`` to the map. This map is intended to be
    passed to ``rewrite_pyproject_deps``.

    Args:
        workspace_root: absolute path to the workspace root
        projects: list of project dicts (each with "name" and "path")
        graph: a WorkspaceGraph instance (unused currently, reserved for
               future constraint refinement)

    Returns a dict mapping package name to version constraint string.
    """
    rewrite_map = {}
    for proj in projects:
        proj_dir = os.path.join(workspace_root, proj["path"])
        targets = detect_targets(proj_dir)
        for entry in targets:
            target = TARGETS.get(entry.name)
            if target is None:
                continue
            try:
                version = target.read_version(entry.path)
            except (VersionError, FileNotFoundError, KeyError):
                continue
            if version:
                rewrite_map[proj["name"]] = f">={version}"
                break  # one version per project is enough
    return rewrite_map
