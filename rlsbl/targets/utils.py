"""Shared name-normalization utilities for release targets, handling package name canonicalization across different registry naming conventions."""

import os
import re
import tomllib

from ..errors import VersionError


def detect_python_package_root(project_dir: str) -> str | None:
    """Return the package root (e.g., 'src/orxt') from hatch config or filesystem.

    Detection order:
    1. Hatch ``[tool.hatch.build.targets.wheel].packages`` in pyproject.toml
    2. uv build-backend ``[tool.uv.build-backend].module-root`` in pyproject.toml
    3. Filesystem: ``src/{underscored}/`` directory
    4. Filesystem: ``{underscored}/`` directory (flat layout)
    5. Filesystem: ``{raw_name}/`` directory
    6. Fallback to underscored project name convention (may not exist on disk)

    Raises VersionError if both ``{underscored}/`` and ``src/{underscored}/``
    exist on disk and no config (hatch or uv) declares the canonical location.

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

    # 2) Check uv build-backend for module-root.
    underscored = name.replace("-", "_")
    uv_backend = data.get("tool", {}).get("uv", {}).get("build-backend", {})
    module_root = uv_backend.get("module-root")
    if module_root and isinstance(module_root, str):
        return os.path.join(module_root, underscored)

    # 3-5) Filesystem detection with ambiguity guard.
    has_flat = os.path.isdir(os.path.join(project_dir, underscored))
    has_src = os.path.isdir(os.path.join(project_dir, "src", underscored))

    if has_flat and has_src:
        raise VersionError(
            f"Ambiguous package layout: both {underscored}/ and src/{underscored}/ "
            f"exist. Add [tool.hatch.build.targets.wheel] packages or "
            f"[tool.uv.build-backend] module-root to pyproject.toml to declare "
            f"the canonical location."
        )

    if has_src:
        return os.path.join("src", underscored)
    if has_flat:
        return underscored
    if os.path.isdir(os.path.join(project_dir, name)):
        return name

    # 6) Fallback to underscored convention (may not exist on disk).
    return underscored


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
