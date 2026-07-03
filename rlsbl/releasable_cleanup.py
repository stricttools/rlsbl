"""Cleanup utilities for per-package .rlsbl/ directories after releasable model migration, removing orphaned changelog and release state files from the old layout.

When a workspace uses explicit-mode releasables, changelog and release state
moves to per-releasable directories under ``.rlsbl-monorepo/releasables/{name}/``.
The per-package ``.rlsbl/changes/`` and ``.rlsbl/releases/`` directories become
dead state that should be removed.

These functions are intended for use by migration tooling (Phase 10), not for
automatic invocation during scaffold or release.
"""

import os
import subprocess

from .config import read_json_config
from .workspace import (
    WorkspaceProject,
    get_releasable_dir,
    is_explicit_mode,
    load_releasables,
    load_workspace,
)


# Files and directories expected to remain in a per-package .rlsbl/ after
# cleanup.  Anything else is unexpected and flagged by verify_minimal_rlsbl().
# config.json is kept only when it differs from the releasable-level config;
# hooks/ stays because per-package script hooks are a live feature (run by
# run_releasable_hooks via get_package_hook_path); bases/, lint/, changes/,
# releases/, and the version marker are all removed during cleanup.
EXPECTED_RLSBL_CONTENTS = frozenset({
    "config.json",
    "hashes.json",
    "managed-files.json",
    "hooks",
})


def cleanup_per_package_release_state(workspace_root, projects=None,
                                      releasables=None, dry_run=False):
    """Remove per-package .rlsbl/ state for packages in explicit-mode releasables.

    Only acts when the workspace is in explicit mode (``[[releasables]]`` is
    defined in workspace.toml).  For each project that belongs to a releasable
    (``releasable`` is a string, not False), removes:

    - ``.rlsbl/changes/`` -- changelog state (moved to releasable level)
    - ``.rlsbl/releases/`` -- release state (moved to releasable level)
    - ``.rlsbl/bases/`` -- merge bases (moved to releasable level)
    - ``.rlsbl/lint/`` -- lint configs (moved to releasable level)
    - ``.rlsbl/version`` -- rlsbl scaffold version file
    - ``CHANGELOG.md`` -- generated changelog (now per-releasable)
    - ``.rlsbl/config.json`` -- only when identical to the releasable-level config

    Exemptions (never removed):

    - ``.rlsbl/hooks/`` -- per-package script hooks are a live feature.
    - Members whose path resolves to the workspace root: the root
      ``.rlsbl/`` and root ``CHANGELOG.md`` are workspace-level files
      (e.g. the combined root changelog), not per-package residue.

    Uses ``saferm`` for removal so there is an audit trail and the files
    are recoverable.

    Args:
        workspace_root: path to the monorepo root (containing .rlsbl-monorepo/).
        projects: optional pre-loaded list of WorkspaceProject. If None, loads
            from workspace.toml.
        releasables: optional pre-loaded list of Releasable. If None, loads
            from workspace.toml.
        dry_run: when True, only collect and return the paths that WOULD be
            removed; nothing is deleted.

    Returns:
        A list of paths that were removed (or would be removed under
        ``dry_run``). Empty if nothing to clean.

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

    # Pre-load releasable-level configs for config.json comparison
    releasable_configs = {}
    for r in releasables:
        rel_dir = get_releasable_dir(workspace_root, r.name)
        rel_config_path = os.path.join(rel_dir, "config.json")
        releasable_configs[r.name] = read_json_config(rel_config_path)

    for proj in projects:
        rel_val = _get_project_releasable(proj)
        # Skip projects not in a releasable (releasable=false or None)
        if not isinstance(rel_val, str):
            continue
        # Skip if the project's releasable isn't actually defined
        if rel_val not in releasable_names:
            continue

        proj_path = os.path.join(workspace_root, proj.path)

        # Root-path members are exempt: their .rlsbl/ and CHANGELOG.md are
        # workspace-level files, not per-package residue.
        if os.path.realpath(proj_path) == os.path.realpath(str(workspace_root)):
            continue

        rlsbl_dir = os.path.join(proj_path, ".rlsbl")

        # Remove directories: changes, releases, bases, lint.
        # hooks/ is exempt (live per-package script hooks feature).
        for subdir in ("changes", "releases", "bases", "lint"):
            target = os.path.join(rlsbl_dir, subdir)
            if os.path.isdir(target):
                if not dry_run:
                    _saferm_dir(target, proj.name, subdir)
                removed.append(target)

        # Remove .rlsbl/version file
        version_file = os.path.join(rlsbl_dir, "version")
        if os.path.isfile(version_file):
            if not dry_run:
                _saferm_file(version_file, proj.name, "version")
            removed.append(version_file)

        # Remove CHANGELOG.md
        changelog_file = os.path.join(proj_path, "CHANGELOG.md")
        if os.path.isfile(changelog_file):
            if not dry_run:
                _saferm_file(changelog_file, proj.name, "CHANGELOG.md")
            removed.append(changelog_file)

        # Remove config.json when identical to releasable-level config
        config_file = os.path.join(rlsbl_dir, "config.json")
        if os.path.isfile(config_file):
            pkg_config = read_json_config(config_file)
            rel_config = releasable_configs.get(rel_val, {})
            if pkg_config == rel_config:
                if not dry_run:
                    _saferm_file(config_file, proj.name, "config.json")
                removed.append(config_file)

    return removed


def run_cleanup_command(workspace_root, *, dry_run=False, yes=False):
    """CLI entry for `rlsbl monorepo cleanup`.

    Removes per-package release-state residue from releasable member
    packages (via :func:`cleanup_per_package_release_state`, which uses
    saferm for an audit trail), then commits the deletions so the working
    tree stays clean.

    Returns the list of removed (or would-be-removed) paths.
    """
    import sys

    candidates = cleanup_per_package_release_state(workspace_root, dry_run=True)
    if not candidates:
        print("Nothing to clean: no per-package release-state residue found.")
        return []

    verb = "Would remove" if dry_run else "Removing"
    for path in candidates:
        print(f"{verb}: {os.path.relpath(path, workspace_root)}")
    if dry_run:
        return candidates

    if not yes:
        try:
            answer = input(
                f"\nRemove {len(candidates)} path(s) via saferm and commit? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            raise SystemExit(1)
        if answer != "y":
            print("Aborted.")
            raise SystemExit(0)

    removed = cleanup_per_package_release_state(workspace_root)

    # Commit the deletions of tracked paths so trees stay clean. Untracked
    # residue (e.g. an uncommitted .rlsbl/version) vanishes without a
    # git-visible change and must not be passed to the commit tool.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *removed],
        capture_output=True, text=True, cwd=str(workspace_root),
    )
    tracked_changes = []
    for line in status.stdout.splitlines():
        if len(line) >= 4:
            tracked_changes.append(line[3:].strip())
    if tracked_changes:
        from .utils import commit_files
        commit_files(
            "chore: remove per-package release-state residue",
            tracked_changes,
            cwd=str(workspace_root),
        )
        print(f"Committed {len(tracked_changes)} deletion(s).")
    else:
        print("No tracked files changed; nothing to commit.", file=sys.stderr)

    return removed


def verify_minimal_rlsbl(project_path):
    """Return unexpected files/dirs in .rlsbl/ for a releasable member package.

    After cleanup, a per-package ``.rlsbl/`` should contain only:

    - ``hashes.json`` (scaffold metadata)
    - ``managed-files.json`` (scaffold metadata)
    - ``config.json`` (only if it has overrides differing from releasable config)
    - ``hooks/`` (per-package script hooks are a live feature)

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


def _saferm_file(path, project_name, file_name):
    """Remove a single file using saferm with an audit trail.

    Raises RuntimeError if saferm is not on PATH.
    Raises subprocess.CalledProcessError if saferm exits non-zero.
    """
    description = (
        f"Removing per-package {file_name} from '{project_name}' "
        f"-- state moved to per-releasable directory"
    )
    try:
        subprocess.run(
            [
                "saferm", "delete",
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
