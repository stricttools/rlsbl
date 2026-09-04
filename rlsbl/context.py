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

def resolve_release_scope(root):
    """Resolve the release/changelog context of the project at *root*.

    Returns ``(project, tag_glob, changes_dir, scope)``:

    - ``project`` is the WorkspaceProject (None in standalone mode).
    - ``tag_glob`` scopes tag-namespace questions to this project's own
      releases.
    - ``changes_dir`` is the releasable-aware JSONL directory, and therefore
      also names the release record (``.../releases/``) that records what this project
      released. A releasable member's entries live under
      ``.rlsbl-monorepo/releasables/<name>/``, NOT under the member package --
      resolving it per-package made every releasable member report "JSONL
      changelog not set up" and read an empty release record.
    - ``scope`` is the ownership scope commits are attributed against: the
      whole releasable -- its members plus its own state directory -- when the
      project is in one, the single member otherwise, and ``None`` outside a
      workspace.

    Shared by every command that answers a question about this project's
    releases -- listing unreleased commits, labelling a watched commit with
    the release it is -- so they all read the same directory and the same tag
    scheme for the same checkout.

    The per-package fallback answers exactly ONE question: this checkout is not
    inside a workspace, or is inside one that does not claim it. Both are plain
    returns below. Everything past them is a workspace that exists and claims
    this project, so a loader or validation failure there is a hard error and
    propagates: degrading it to the fallback would answer with the member
    package's own (empty) changes directory and no tag scheme, and the caller
    would report a coverage figure and an unreleased range for a project it had
    in fact failed to identify.
    """
    from .changelog import get_changes_dir
    from .workspace import find_workspace_root, load_workspace, resolve_project

    root_str = str(root)
    project = None
    tag_glob = None
    changes_dir = get_changes_dir(root_str)
    scope = None

    ws_root = find_workspace_root(root_str)
    if ws_root is None:
        return project, tag_glob, changes_dir, scope
    project = resolve_project(ws_root, root_str)
    if project is None:
        return project, tag_glob, changes_dir, scope

    from .ownership import OwnershipScope
    from .tag_glob import releasable_tag_glob
    from .targets import TARGETS, detect_targets, resolve_releasable_config_dir
    from .workspace import (
        get_releasable_changes_dir,
        load_releasables,
        members_of,
        resolve_releasable_for_project,
    )

    ws_projects = load_workspace(ws_root)
    releasables = load_releasables(ws_root, ws_projects)
    rel = resolve_releasable_for_project(project, releasables)

    if rel is not None:
        tag_glob = releasable_tag_glob(rel.effective_tag_format, rel.name)
        changes_dir = get_releasable_changes_dir(ws_root, rel.name)
        scope = OwnershipScope.for_releasable(
            ws_projects, members_of(rel.name, ws_projects), rel.name,
        )
    else:
        scope = OwnershipScope.for_member(ws_projects, project)
        rel_dir = resolve_releasable_config_dir(project, ws_root)
        targets = detect_targets(root_str, releasable_config_dir=rel_dir)
        if targets:
            target = TARGETS[targets[0].name]
            tag_glob = target.monorepo_tag_glob(
                project["name"], path=project["path"],
            )
    return project, tag_glob, changes_dir, scope
