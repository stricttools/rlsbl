"""Batch release command for monorepo workspaces.

Reads .rlsbl-monorepo/releases/unreleased.toml, validates all listed packages,
determines topological release order, and releases each package sequentially
by delegating to the existing single-package release flow.
"""

import os
import stat
import sys
import time

from ...release_file import (
    BatchReleaseConfig,
    get_batch_release_file_path,
    read_batch_release_file,
)
from ...errors import ReleaseFileError
from ...utils import commit_files
from ...workspace import find_workspace_root, load_workspace
from ...workspace_graph import CycleError, WorkspaceGraph


def _cmd_batch_release(flags, project_root):
    """Execute a batch release of multiple monorepo packages."""
    start = str(project_root)
    workspace_root = find_workspace_root(start)
    if workspace_root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    batch_path = get_batch_release_file_path(workspace_root)
    if not os.path.exists(batch_path):
        print(
            "Error: No batch release file found at "
            f"{os.path.relpath(batch_path)}.\n"
            "Create .rlsbl-monorepo/releases/unreleased.toml with "
            "[packages.<name>] sections.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        batch_config = read_batch_release_file(batch_path)
    except ReleaseFileError as e:
        print(f"Error in batch release file: {e}", file=sys.stderr)
        sys.exit(1)

    # Load workspace and build graph
    projects = load_workspace(workspace_root)
    project_names = {p["name"] for p in projects}
    project_by_name = {p["name"]: p for p in projects}

    # Validate all packages exist in workspace
    missing = set(batch_config.packages.keys()) - project_names
    if missing:
        print(
            f"Error: packages not found in workspace: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Reject dev_node projects -- they must be released individually
    dev_nodes_in_batch = sorted(
        name
        for name in batch_config.packages
        if project_by_name[name].get("dev_node", False)
    )
    if dev_nodes_in_batch:
        print(
            "Error: dev_node projects cannot be in batch release: "
            f"{', '.join(dev_nodes_in_batch)}. "
            "Remove dev_node = true from workspace.toml if these projects "
            "should be releasable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine topological order for the listed packages
    graph = WorkspaceGraph(workspace_root, projects)
    try:
        full_order = graph.topological_order()
    except CycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Filter to only the packages in the batch, preserving topological order
    batch_names = set(batch_config.packages.keys())
    release_order = [name for name in full_order if name in batch_names]

    dry_run = flags.get("dry-run", False)
    yes = flags.get("yes", False)
    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    log(f"Batch release: {len(release_order)} package(s)")
    log(f"Release order: {', '.join(release_order)}")
    log("")

    # Release each package in order
    released = []
    for pkg_name in release_order:
        release_config = batch_config.packages[pkg_name]
        project = project_by_name[pkg_name]
        project_dir = os.path.join(workspace_root, project["path"])

        log(f"--- Releasing {pkg_name} ({release_config.bump}) ---")

        try:
            from pathlib import Path

            from ...context import create_context
            from ..release import run_cmd

            release_flags = {
                "dry-run": dry_run,
                "yes": yes,
                "quiet": quiet,
                "allow-dirty": flags.get("allow-dirty", False),
            }
            pkg_ctx = create_context(Path(project_dir), workspace_root=Path(workspace_root))
            run_cmd(release_config, release_flags, ctx=pkg_ctx)
            released.append(pkg_name)
        except SystemExit as e:
            if e.code != 0:
                print(
                    f"\nError: release of {pkg_name} failed. "
                    f"Successfully released: {', '.join(released) if released else '(none)'}",
                    file=sys.stderr,
                )
                raise

        log("")

    # Finalize the batch release file (skip in dry-run)
    if not dry_run and released:
        _finalize_batch_file(batch_path, log)

    log(f"Batch release complete: {', '.join(released)}")


def _finalize_batch_file(batch_path, log):
    """Rename the batch release file to a timestamped name and lock it."""
    releases_dir = os.path.dirname(batch_path)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    versioned_name = f"batch-{timestamp}.toml"
    versioned_path = os.path.join(releases_dir, versioned_name)

    os.rename(batch_path, versioned_path)
    os.chmod(versioned_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 444

    # Create a fresh empty unreleased.toml
    with open(batch_path, "w", encoding="utf-8") as f:
        pass  # empty file

    # Commit finalized files
    finalize_files = [
        os.path.normpath(versioned_path),
        os.path.normpath(batch_path),
    ]
    commit_files(
        f"chore: finalize batch release file ({versioned_name})",
        finalize_files,
        allow_failure=True,
    )
    log(f"Finalized batch release file: {versioned_name}")
