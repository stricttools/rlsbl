"""Project context for general command use."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Releasable, WorkspaceProject


@dataclass
class ProjectContext:
    """Context object carrying project root, optional workspace root, and loaded config."""

    project_root: Path
    workspace_root: Path | None
    config: dict
    project: WorkspaceProject | None = field(default=None)
    push_stdin: str | None = field(default=None)
    releasable: "Releasable | None" = field(default=None)


def _resolve_releasable_config_dir(
    root: Path, workspace_root: Path
) -> str | None:
    """Find the releasable config directory for a package in a monorepo.

    Loads the workspace, resolves which project ``root`` belongs to,
    checks its ``releasable`` field, and returns the releasable state
    directory path if the project belongs to a releasable.

    Returns None if the project is not in the workspace, is not
    releasable, or if ``[[releasables]]`` is not defined.
    """
    import os

    from .workspace import (
        get_releasable_dir,
        is_explicit_mode,
        load_releasables,
        load_workspace,
        resolve_releasable_for_project,
    )

    ws_root = str(workspace_root)
    if not is_explicit_mode(ws_root):
        return None

    try:
        projects = load_workspace(ws_root)
        releasables = load_releasables(ws_root, projects=projects)
    except Exception:
        return None

    # Find which project this root corresponds to
    abs_root = os.path.realpath(str(root))
    matched_project = None
    for proj in projects:
        proj_abs = os.path.realpath(os.path.join(ws_root, proj.path))
        if abs_root == proj_abs:
            matched_project = proj
            break

    if matched_project is None:
        return None

    rel = resolve_releasable_for_project(matched_project, releasables)
    if rel is None:
        return None

    return get_releasable_dir(ws_root, rel.name)


def create_context(
    root: Path,
    workspace_root: Path | None = None,
    project: WorkspaceProject | None = None,
) -> ProjectContext:
    """Create a ProjectContext, loading config via read_project_config().

    When in a monorepo with ``[[releasables]]``, automatically detects
    releasable membership and applies config inheritance (releasable-level
    config as base, per-package config on top).

    Uses the merged view (publish.json + config.json with conflict detection).
    Returns an empty dict for config if neither file exists.
    """
    from .config import read_project_config

    releasable_config_dir = None
    if workspace_root is not None:
        releasable_config_dir = _resolve_releasable_config_dir(root, workspace_root)

    config = read_project_config(root, releasable_config_dir=releasable_config_dir)
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config,
        project=project,
    )
