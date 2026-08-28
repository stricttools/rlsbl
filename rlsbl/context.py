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
    root: Path,
    workspace_root: Path,
    *,
    projects,
    releasables,
    resolve_releasable_for_project_fn,
    get_releasable_dir_fn,
) -> str | None:
    """Find the releasable config directory for a package in a monorepo.

    Receives pre-loaded workspace data via parameters to avoid importing
    from workspace.py (breaks the context->workspace circular dep edge).

    Returns None if the project is not in the workspace, is not
    releasable, or if the workspace could not be loaded.
    """
    import os

    ws_root = str(workspace_root)
    if projects is None or releasables is None:
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

    rel = resolve_releasable_for_project_fn(matched_project, releasables)
    if rel is None:
        return None

    return get_releasable_dir_fn(ws_root, rel.name)


def resolve_releasable_config_dir(root: Path, workspace_root: Path) -> str | None:
    """Convenience wrapper: load workspace data and resolve the releasable config dir.

    External callers (release_state, sync) use this instead of calling
    _resolve_releasable_config_dir directly. The lazy workspace import
    happens here, keeping the inner function import-free.
    """
    from .workspace import (
        get_releasable_dir,
        load_releasables,
        load_workspace,
        resolve_releasable_for_project,
    )

    ws_root = str(workspace_root)
    projects_loaded = None
    releasables_loaded = None
    try:
        projects_loaded = load_workspace(ws_root)
        releasables_loaded = load_releasables(ws_root, projects=projects_loaded)
    except OSError:
        # The ONLY reason to answer None here: the workspace file is not
        # there to read (a race against a removal, or a directory that
        # only looked like a workspace root). That is genuinely "this
        # directory belongs to no releasable".
        #
        # A loader or validation error is NOT that, and used to be caught
        # by a bare `except Exception` that returned None. Callers read
        # None as "no releasable", so a refused workspace looked like a
        # standalone one: a generated publish router came out with its
        # releasable template variables unrendered, and the error
        # explaining why was never raised. Let it through.
        return None

    return _resolve_releasable_config_dir(
        root,
        workspace_root,
        projects=projects_loaded,
        releasables=releasables_loaded,
        resolve_releasable_for_project_fn=resolve_releasable_for_project,
        get_releasable_dir_fn=get_releasable_dir,
    )


def create_context(
    root: Path,
    workspace_root: Path | None = None,
    project: WorkspaceProject | None = None,
) -> ProjectContext:
    """Create a ProjectContext, loading config via read_project_config().

    When in a monorepo with ``[[releasables]]``, automatically detects
    releasable membership and applies config inheritance (releasable-level
    config as base, per-package config on top).

    Returns an empty dict for config if no config.json exists.
    """
    from .config import read_project_config

    releasable_config_dir = None
    if workspace_root is not None:
        # Lazy import: workspace functions are only needed when in a monorepo.
        # The import happens here (in the caller) rather than in
        # _resolve_releasable_config_dir to keep that function free of
        # workspace imports and break the context->workspace edge.
        from .workspace import (
            get_releasable_dir,
            load_releasables,
            load_workspace,
            resolve_releasable_for_project,
        )

        ws_root = str(workspace_root)
        projects_loaded = None
        releasables_loaded = None
        try:
            projects_loaded = load_workspace(ws_root)
            releasables_loaded = load_releasables(ws_root, projects=projects_loaded)
        except Exception:
            pass

        releasable_config_dir = _resolve_releasable_config_dir(
            root,
            workspace_root,
            projects=projects_loaded,
            releasables=releasables_loaded,
            resolve_releasable_for_project_fn=resolve_releasable_for_project,
            get_releasable_dir_fn=get_releasable_dir,
        )

    config = read_project_config(root, releasable_config_dir=releasable_config_dir)
    return ProjectContext(
        project_root=root,
        workspace_root=workspace_root,
        config=config,
        project=project,
    )
