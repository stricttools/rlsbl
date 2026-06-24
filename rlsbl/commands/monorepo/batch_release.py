"""Batch release command for monorepo workspaces.

Reads .rlsbl-monorepo/releases/unreleased.toml, validates all listed items,
determines topological release order, and releases each sequentially
by delegating to the existing single-package release flow.

In explicit mode (``[releasables.*]`` sections), iterates releasables in
dependency order (a releasable's position = max topological position of
its member packages). For each releasable, picks one representative member
package and releases through it.

In implicit mode (``[packages.*]`` sections), iterates packages directly
in topological order (original behavior).
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
from ...lock import rlsbl_lock
from ...utils import commit_files, run
from ...workspace import find_workspace_root, load_workspace, is_explicit_mode
from ...workspace_graph import CycleError, WorkspaceGraph
from ..release.validate import (
    ReleaseValidationError,
    validate_branch_and_remote,
    validate_clean_tree,
    validate_gh_cli,
)


def _releasable_release_order(batch_names, releasables, projects, graph):
    """Compute release order for releasables based on member topological positions.

    A releasable's position is the maximum topological position of its member
    packages. This ensures that a releasable whose members depend on members
    of another releasable is released after the dependency.

    Returns an ordered list of releasable names from the batch.
    """
    from ...workspace import members_of

    full_order = graph.topological_order()
    position = {name: i for i, name in enumerate(full_order)}

    releasable_positions = {}
    for rel in releasables:
        if rel.name not in batch_names:
            continue
        members = members_of(rel.name, projects)
        if members:
            max_pos = max(position.get(m["name"], 0) for m in members)
        else:
            max_pos = 0
        releasable_positions[rel.name] = max_pos

    return sorted(batch_names, key=lambda n: releasable_positions.get(n, 0))


def _cmd_batch_release(flags, project_root):
    """Execute a batch release of multiple monorepo packages or releasables."""
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
            "[packages.<name>] or [releasables.<name>] sections.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        batch_config = read_batch_release_file(batch_path)
    except ReleaseFileError as e:
        print(f"Error in batch release file: {e}", file=sys.stderr)
        sys.exit(1)

    # Upfront validation: fail before releasing anything
    try:
        validate_gh_cli()
        validate_clean_tree(flags)
        validate_branch_and_remote(flags)
    except ReleaseValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(workspace_root)
    explicit = is_explicit_mode(workspace_root)

    if batch_config.section_type == "releasables":
        if not explicit:
            print(
                "Error: batch release file uses [releasables] sections but the "
                "workspace is in implicit mode (no [[releasables]] in workspace.toml).",
                file=sys.stderr,
            )
            sys.exit(1)
        _batch_release_releasables(
            flags, workspace_root, batch_path, batch_config, projects,
        )
    else:
        _batch_release_packages(
            flags, workspace_root, batch_path, batch_config, projects,
        )


def _batch_release_releasables(flags, workspace_root, batch_path, batch_config, projects):
    """Execute batch release in releasable mode."""
    from ...workspace import load_releasables, members_of

    releasables = load_releasables(workspace_root, projects)
    releasable_by_name = {r.name: r for r in releasables}

    # Validate all releasable names exist
    missing = set(batch_config.packages.keys()) - set(releasable_by_name.keys())
    if missing:
        print(
            f"Error: releasables not found in workspace: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build graph and compute release order
    graph = WorkspaceGraph(workspace_root, projects)
    try:
        graph.topological_order()
    except CycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    batch_names = set(batch_config.packages.keys())
    release_order = _releasable_release_order(
        batch_names, releasables, projects, graph,
    )

    dry_run = flags.get("dry-run", False)
    yes = flags.get("yes", False)
    quiet = flags.get("quiet", False)

    def log(msg):
        if not quiet:
            print(msg)

    log(f"Batch release: {len(release_order)} releasable(s)")
    log(f"Release order: {', '.join(release_order)}")

    if dry_run:
        for i, name in enumerate(release_order, 1):
            rc = batch_config.packages[name]
            log(f"  {i}. {name} ({rc.bump}) — {rc.description}")

    log("")

    released = []
    with rlsbl_lock(".rlsbl-monorepo", project_root=workspace_root):
        for rel_name in release_order:
            release_config = batch_config.packages[rel_name]
            member_projs = members_of(rel_name, projects)
            if not member_projs:
                print(f"Error: releasable '{rel_name}' has no member projects.", file=sys.stderr)
                sys.exit(1)

            # Pick the first member as the representative for the release flow
            representative = member_projs[0]
            project_dir = os.path.join(workspace_root, representative["path"])

            log(f"--- Releasing releasable {rel_name} ({release_config.bump}) ---")

            try:
                from pathlib import Path

                from ...context import create_context
                from ..release import run_cmd

                release_flags = {
                    "dry-run": dry_run,
                    "yes": yes,
                    "quiet": quiet,
                    "allow-dirty": flags.get("allow-dirty", False),
                    "skip-lock": True,
                    "batch-mode": True,
                }
                pkg_ctx = create_context(Path(project_dir), workspace_root=Path(workspace_root))
                run_cmd(release_config, release_flags, ctx=pkg_ctx)
                released.append(rel_name)
                if not dry_run:
                    last_sha = run("git", ["rev-parse", "HEAD"])
            except SystemExit as e:
                if e.code != 0:
                    print(
                        f"\nError: release of releasable {rel_name} failed. "
                        f"Successfully released: {', '.join(released) if released else '(none)'}",
                        file=sys.stderr,
                    )
                    raise

            log("")

        if not dry_run and released:
            _finalize_batch_file(batch_path, log)

            # Watch CI or print hint for the last release's commit
            if flags.get("watch"):
                log(f"Watching CI for {last_sha}...")
                from ..watch import run_cmd as watch_run_cmd
                watch_run_cmd(None, [last_sha], {})
            else:
                log(f"Watch CI: rlsbl watch {last_sha}")

    log(f"Batch release complete: {', '.join(released)}")


def _batch_release_packages(flags, workspace_root, batch_path, batch_config, projects):
    """Execute batch release in package mode (implicit, original behavior)."""
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

    # Reject non-releasable projects
    non_releasable_in_batch = sorted(
        name
        for name in batch_config.packages
        if not project_by_name[name].is_releasable
    )
    if non_releasable_in_batch:
        print(
            "Error: non-releasable projects cannot be in batch release: "
            f"{', '.join(non_releasable_in_batch)}. "
            "Set releasable = \"<name>\" in workspace.toml if these projects "
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

    if dry_run:
        for i, name in enumerate(release_order, 1):
            rc = batch_config.packages[name]
            log(f"  {i}. {name} ({rc.bump}) — {rc.description}")

    log("")

    # Release each package in order
    released = []
    with rlsbl_lock(".rlsbl-monorepo", project_root=workspace_root):
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
                    "skip-lock": True,
                    "batch-mode": True,
                }
                pkg_ctx = create_context(Path(project_dir), workspace_root=Path(workspace_root))
                run_cmd(release_config, release_flags, ctx=pkg_ctx)
                released.append(pkg_name)
                if not dry_run:
                    last_sha = run("git", ["rev-parse", "HEAD"])
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

            # Watch CI or print hint for the last release's commit
            if flags.get("watch"):
                log(f"Watching CI for {last_sha}...")
                from ..watch import run_cmd as watch_run_cmd
                watch_run_cmd(None, [last_sha], {})
            else:
                log(f"Watch CI: rlsbl watch {last_sha}")

    log(f"Batch release complete: {', '.join(released)}")


def _finalize_batch_file(batch_path, log):
    """Rename the batch release file to a timestamped name and lock it."""
    releases_dir = os.path.dirname(batch_path)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    versioned_name = f"batch-{timestamp}.toml"
    versioned_path = os.path.join(releases_dir, versioned_name)

    os.rename(batch_path, versioned_path)
    os.chmod(versioned_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 444

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
