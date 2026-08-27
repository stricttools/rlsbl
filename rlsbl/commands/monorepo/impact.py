"""Monorepo impact analysis command: show affected packages from a change."""

import os
import subprocess
import sys

from ...ownership import owner_name_of
from ...workspace import find_workspace_root, load_workspace
from ...workspace_graph import WorkspaceGraph
from ... import effects


def _map_file_to_package(file_path, projects, root):
    """Map a file path (relative to repo root) to its owning package name.

    The one attribution rule, from :mod:`rlsbl.ownership`: the most specific
    declared member path wins, the root member owns the residual, and a
    tool-owned path (changelog state, the workspace directory, the generated
    router) belongs to no package.  Returns the package name, or ``None``.
    """
    return owner_name_of(file_path, projects)


def _get_changed_files_from_git(since_ref, root):
    """Run git diff --name-only {since_ref}..HEAD and return file paths."""
    try:
        result = effects.run(
            ["git", "--no-optional-locks", "diff", "--name-only", f"{since_ref}..HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: git diff failed: {exc.stderr.strip() or exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    lines = result.stdout.strip().splitlines()
    return [line.strip() for line in lines if line.strip()]


def _get_affected_packages_from_git(since_ref, projects, root):
    """Map git-changed files to workspace package names."""
    changed_files = _get_changed_files_from_git(since_ref, root)
    package_names = set()
    for fpath in changed_files:
        name = _map_file_to_package(fpath, projects, root)
        if name is not None:
            package_names.add(name)
    return package_names


def _is_file_path(arg):
    """Heuristic: does the argument look like a file path rather than a package name?"""
    return os.sep in arg or "/" in arg or "." in os.path.basename(arg)


def _compute_impact(package_names, graph, depth):
    """Compute direct and transitive dependents for a set of packages.

    Returns a dict with: input, direct_dependents, transitive_dependents,
    test_scope, release_candidates.
    """
    all_direct = set()
    all_transitive = set()
    for name in package_names:
        direct = graph.dependents(name)
        all_direct.update(direct)
        transitive = graph.transitive_rdeps(name, depth=depth)
        all_transitive.update(transitive)

    # Sort for deterministic output
    direct_sorted = sorted(all_direct)
    transitive_sorted = sorted(all_transitive)

    return {
        "input": ", ".join(sorted(package_names)),
        "direct_dependents": direct_sorted,
        "transitive_dependents": transitive_sorted,
        "test_scope": transitive_sorted,
        "release_candidates": transitive_sorted,
    }


def _render_text(impact_data):
    """Format impact data as human-readable text."""
    lines = []
    lines.append(f"Impact analysis for: {impact_data['input']}")
    lines.append("")

    direct = impact_data["direct_dependents"]
    lines.append(f"Direct dependents ({len(direct)}):")
    if direct:
        for name in direct:
            lines.append(f"  {name}")
    else:
        lines.append("  (none)")
    lines.append("")

    transitive = impact_data["transitive_dependents"]
    lines.append(f"Transitive dependents ({len(transitive)}):")
    if transitive:
        for name in transitive:
            lines.append(f"  {name}")
    else:
        lines.append("  (none)")
    lines.append("")

    if transitive:
        scope_str = ", ".join(transitive)
        lines.append(f"Test scope: {scope_str}")
        lines.append(f"Release candidates: {scope_str}")
    else:
        lines.append("Test scope: (none)")
        lines.append("Release candidates: (none)")

    return "\n".join(lines)


def _cmd_impact(args, flags, project_root):
    """Analyze the impact of changes on the monorepo dependency graph.

    Returns the impact report -- the caller (the CLI handler) hands it to the
    framework as the command's machine payload -- or None when there is
    nothing to report.  The human rendering is printed here, except in machine
    mode, where the envelope owns stdout.
    """
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return None

    graph = WorkspaceGraph(root, projects)

    since_ref = flags.get("since") or None
    depth_raw = flags.get("depth")
    depth = int(depth_raw) if depth_raw is not None else None

    project_names_in_graph = {p["name"] for p in projects}
    package_names = set()

    if since_ref:
        # Mode 3: git diff
        package_names = _get_affected_packages_from_git(since_ref, projects, root)
    elif args:
        # Determine if args are file paths or package names
        if any(_is_file_path(a) for a in args):
            # Mode 2: file paths
            for fpath in args:
                name = _map_file_to_package(fpath, projects, root)
                if name is not None:
                    package_names.add(name)
                else:
                    print(
                        f"Warning: '{fpath}' does not belong to any workspace package.",
                        file=sys.stderr,
                    )
        else:
            # Mode 1: package names
            for name in args:
                if name not in project_names_in_graph:
                    print(
                        f"Error: package '{name}' not found in workspace.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                package_names.add(name)
    else:
        print(
            "Error: provide a package name, file path, or --since flag.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not package_names:
        print("No affected packages found.")
        return None

    impact_data = _compute_impact(package_names, graph, depth)

    # In machine mode the envelope is stdout's only document, so the human
    # rendering is not printed.
    if not flags.get("json"):
        print(_render_text(impact_data))

    return impact_data
