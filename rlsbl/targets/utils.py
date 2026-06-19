"""Shared name-normalization utilities for release targets, handling package name canonicalization across different registry naming conventions."""

import os
import re
import tomllib


def detect_python_package_root(project_dir: str) -> str | None:
    """Return the package root (e.g., 'src/orxt') from hatch config or filesystem.

    Detection order:
    1. Hatch ``[tool.hatch.build.targets.wheel].packages`` in pyproject.toml
    2. Filesystem: directory matching underscored project name, then raw name
    3. Fallback to underscored project name convention (may not exist on disk)

    Returns the relative path from project_dir to the package root directory,
    or None if pyproject.toml is missing or the project name cannot be read.
    """
    pyproject_path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return None

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None

    project = data.get("project", {})
    name = project.get("name", "")
    if not name:
        return None

    # 1) Check hatch build config for an explicit packages list.
    hatch = data.get("tool", {}).get("hatch", {})
    packages = (
        hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages")
    )
    if packages and isinstance(packages, list) and len(packages) > 0:
        return packages[0]

    # 2) Fall back to filesystem detection, then underscore convention.
    underscored = name.replace("-", "_")
    if os.path.isdir(os.path.join(project_dir, underscored)):
        return underscored
    elif os.path.isdir(os.path.join(project_dir, name)):
        return name
    else:
        return underscored  # fallback to convention


def normalize_npm(name):
    """Normalize an npm package name for similarity comparison.

    Strips hyphens, underscores, dots, and lowercases.
    """
    return re.sub(r"[-_.]", "", name.lower())


def normalize_pypi(name):
    """Normalize a PyPI package name per PEP 503.

    Lowercases and replaces runs of [-_.] with a single hyphen.
    """
    return re.sub(r"[-_.]+", "-", name.lower())


def normalize_go(name):
    """Normalize a Go module path to a short name.

    Takes a module path like 'github.com/user/repo' and returns the last
    path segment lowercased (e.g., 'repo').
    """
    segment = name.rsplit("/", 1)[-1] if "/" in name else name
    return segment.lower()


def _get_git_author() -> str:
    """Return the git config user.name, or empty string on failure."""
    from ..utils import run
    try:
        return run("git", ["config", "user.name"])
    except Exception:
        return ""
