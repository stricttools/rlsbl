"""Cleanup utilities for per-package .rlsbl/ directories after releasable model migration.

When a workspace uses explicit-mode releasables, changelog and release state
moves to per-releasable directories under ``.rlsbl-monorepo/releasables/{name}/``.
The per-package ``.rlsbl/changes/`` and ``.rlsbl/releases/`` directories become
dead state that should be removed.

These functions are intended for use by migration tooling (Phase 10), not for
automatic invocation during scaffold or release.
"""

import os
import subprocess

from .workspace import (
    WorkspaceProject,
    is_explicit_mode,
    load_releasables,
    load_workspace,
    members_of,
)


# Files and directories expected to remain in a per-package .rlsbl/ after
# cleanup.  Anything else is unexpected and flagged by verify_minimal_rlsbl().
EXPECTED_RLSBL_CONTENTS = frozenset({
    "publish.json",
    "config.json",
    "hooks",
    "hashes.json",
    "managed-files.json",
})


def cleanup_per_package_release_state(workspace_root, projects=None, releasables=None):
    """Remove .rlsbl/changes/ and .rlsbl/releases/ for packages in explicit-mode releasables.

    Only acts when the workspace is in explicit mode (``[[releasables]]`` is
    defined in workspace.toml).  For each project that belongs to a releasable
    (``releasable`` is a string, not False), removes the per-package changelog
    and release directories if they exist.

    Uses ``saferm delete -r`` for removal so there is an audit trail and
    the files are recoverable.

    Args:
        workspace_root: path to the monorepo root (containing .rlsbl-monorepo/).
        projects: optional pre-loaded list of WorkspaceProject. If None, loads
            from workspace.toml.
        releasables: optional pre-loaded list of Releasable. If None, loads
            from workspace.toml.

    Returns:
        A list of paths that were removed (empty if nothing to clean).

    Raises:
        RuntimeError: if saferm is not available on PATH.
        subprocess.CalledProcessError: if saferm fails for a path.
    """
    if not is_explicit_mode(workspace_root):
        return []

    if projects is None:
        projects = load_workspace(workspace_root)
    if releasables is None:
        releasables = load_releasables(workspace_root, projects=projects)

    releasable_names = {r.name for r in releasables}
    removed = []

    for proj in projects:
        rel_val = _get_project_releasable(proj)
        # Skip projects not in a releasable (releasable=false or None)
        if not isinstance(rel_val, str):
            continue
        # Skip if the project's releasable isn't actually defined
        if rel_val not in releasable_names:
            continue

        proj_path = os.path.join(workspace_root, proj.path)
        for subdir in ("changes", "releases"):
            target = os.path.join(proj_path, ".rlsbl", subdir)
            if os.path.isdir(target):
                _saferm_dir(target, proj.name, subdir)
                removed.append(target)

    return removed


def verify_minimal_rlsbl(project_path):
    """Return unexpected files/dirs in .rlsbl/ for a releasable member package.

    After cleanup, a per-package ``.rlsbl/`` should contain only:

    - ``publish.json`` (publishing config)
    - ``config.json`` (non-publishing config fields)
    - ``hooks/`` (per-package hooks)
    - ``hashes.json`` (scaffold metadata)
    - ``managed-files.json`` (scaffold metadata)

    Args:
        project_path: absolute or relative path to the project directory.

    Returns:
        A list of unexpected file/directory names found in ``.rlsbl/``.
        Empty list means the directory is clean.
    """
    rlsbl_dir = os.path.join(project_path, ".rlsbl")
    if not os.path.isdir(rlsbl_dir):
        return []

    unexpected = []
    for entry in sorted(os.listdir(rlsbl_dir)):
        if entry not in EXPECTED_RLSBL_CONTENTS:
            unexpected.append(entry)

    return unexpected


def _get_project_releasable(proj):
    """Extract the releasable value from a project.

    Returns str, False, or None.
    """
    if isinstance(proj, WorkspaceProject):
        return proj.releasable
    return proj.get("releasable")


def _saferm_dir(path, project_name, subdir_name):
    """Remove a directory using saferm with an audit trail.

    Raises RuntimeError if saferm is not on PATH.
    Raises subprocess.CalledProcessError if saferm exits non-zero.
    """
    description = (
        f"Removing per-package .rlsbl/{subdir_name}/ from '{project_name}' "
        f"-- release state moved to per-releasable directory"
    )
    try:
        subprocess.run(
            [
                "saferm", "delete", "-r",
                "--description", description,
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "saferm is not installed or not on PATH. "
            "Install saferm before running cleanup."
        ) from None
