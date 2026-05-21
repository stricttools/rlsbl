"""Monorepo impact analysis command: show affected packages from a change."""

import json
import os
import subprocess
import sys

from ...workspace import find_workspace_root, load_workspace
from ...workspace_graph import WorkspaceGraph


def _map_file_to_package(file_path, projects, root):
    """Map a file path (relative to repo root) to its containing package name.

    Checks which project's path is a prefix of the file path.  When multiple
    projects match (nested paths), the most specific (longest) prefix wins.
    Returns the project name or None if no project matches.
    """
    # Normalize separators
    file_path = file_path.replace("\\", "/").rstrip("/")
    best_name = None
    best_len = -1
    for proj in projects:
        proj_path = proj["path"].replace("\\", "/").rstrip("/")
        if file_path == proj_path or file_path.startswith(proj_path + "/"):
            if len(proj_path) > best_len:
                best_name = proj["name"]
                best_len = len(proj_path)
    return best_name


def _get_changed_files_from_git(since_ref, root):
    """Run git diff --name-only {since_ref}..HEAD and return file paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{since_ref}..HEAD"],
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


def _render_json(impact_data):
    """Format impact data as JSON."""
    return json.dumps(impact_data, indent=2)


def _cmd_impact(args, flags):
    """Analyze the impact of changes on the monorepo dependency graph."""
    root = find_workspace_root(".")
    if root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    graph = WorkspaceGraph(root, projects)

    since_ref = flags.get("since") or None
    fmt = flags.get("format", "text")
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
        return

    impact_data = _compute_impact(package_names, graph, depth)

    if fmt == "json":
        print(_render_json(impact_data))
    elif fmt == "text":
        print(_render_text(impact_data))
    else:
        print(
            f"Error: unknown format '{fmt}'. Use json or text.",
            file=sys.stderr,
        )
        sys.exit(1)
