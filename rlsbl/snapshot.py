"""Monorepo snapshot generator: produces a JSON summary of all packages, versions, deps, and graph structure."""

import json
import os
from datetime import datetime, timezone

from .targets import detect_targets, TARGETS
from .workspace import WORKSPACE_DIR


SNAPSHOT_FILE = "snapshot.json"


def generate_snapshot(root, projects, graph):
    """Build the snapshot dict from workspace data.

    Args:
        root: absolute path to the monorepo root.
        projects: list of project dicts from load_workspace().
        graph: WorkspaceGraph instance.

    Returns a dict matching the snapshot schema.
    """
    topo_order = graph.topological_order()

    packages = {}
    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect target
        target_entries = detect_targets(os.path.join(root, path))
        target_name = target_entries[0].name if target_entries else None

        # Read version
        version = None
        if target_name and target_name in TARGETS:
            target_path = target_entries[0].path if target_entries else os.path.join(root, path)
            try:
                version = TARGETS[target_name].read_version(target_path)
            except Exception:
                version = None

        deps = [d.name for d in graph.dependencies(name)]
        rdeps = graph.dependents(name)

        packages[name] = {
            "path": path,
            "target": target_name,
            "version": version,
            "description": proj.get("description"),
            "deps": deps,
            "rdeps": rdeps,
            "library": proj.get("library", False),
            "changelog_exempt": proj.get("changelog_exempt", False),
            "test_only": proj.get("test_only", False),
        }

    # Compute graph metadata
    leaf_nodes = sorted(
        name for name in packages if graph.dep_count(name) == 0
    )
    root_nodes = sorted(
        name for name in packages if graph.rdep_count(name) == 0
    )
    max_depth = _compute_max_depth(packages, graph, topo_order)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "package_count": len(packages),
        "packages": packages,
        "graph": {
            "leaf_nodes": leaf_nodes,
            "root_nodes": root_nodes,
            "max_depth": max_depth,
            "topological_order": topo_order,
        },
    }


def _compute_max_depth(packages, graph, topo_order):
    """Compute the longest path in the DAG using topological order.

    Leaf nodes (no deps) have depth 0. Each node's depth is
    max(depth of all deps) + 1. Returns the maximum across all nodes.
    """
    if not topo_order:
        return 0

    depth = {}
    for name in topo_order:
        deps = [d.name for d in graph.dependencies(name)]
        if not deps:
            depth[name] = 0
        else:
            depth[name] = max(depth.get(d, 0) for d in deps) + 1

    return max(depth.values()) if depth else 0


def write_snapshot(root, snapshot):
    """Write the snapshot dict to .rlsbl-monorepo/snapshot.json.

    Args:
        root: monorepo root path.
        snapshot: the snapshot dict.

    Returns the relative path to the written file.
    """
    ws_dir = os.path.join(root, WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    target = os.path.join(ws_dir, SNAPSHOT_FILE)

    content = json.dumps(snapshot, indent=2) + "\n"
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, target)

    return os.path.join(WORKSPACE_DIR, SNAPSHOT_FILE)


def check_snapshot(root, projects, graph):
    """Check whether the on-disk snapshot is up-to-date.

    Compares the existing snapshot.json against a freshly generated one,
    ignoring the generated_at timestamp. Returns True if they match.
    """
    snapshot_path = os.path.join(root, WORKSPACE_DIR, SNAPSHOT_FILE)
    if not os.path.isfile(snapshot_path):
        return False

    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    fresh = generate_snapshot(root, projects, graph)

    # Compare everything except generated_at
    existing_cmp = {k: v for k, v in existing.items() if k != "generated_at"}
    fresh_cmp = {k: v for k, v in fresh.items() if k != "generated_at"}

    return existing_cmp == fresh_cmp
