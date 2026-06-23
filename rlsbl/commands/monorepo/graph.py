"""Monorepo dependency graph export command: JSON, DOT, and text tree formats."""

import json
import os
import sys

from ...workspace import find_workspace_root, load_workspace
from ...workspace_graph import WorkspaceGraph
from ...targets import detect_targets, resolve_releasable_config_dir, TARGETS


def _collect_graph_data(root, projects, graph):
    """Build packages dict and edges list from the workspace graph.

    Returns (packages, edges) where packages is a dict keyed by project name
    and edges is a list of dicts with from/to/type/constraint keys.
    """
    packages = {}
    edges = []

    for proj in projects:
        name = proj["name"]
        path = proj["path"]

        # Detect targets
        rel_dir = resolve_releasable_config_dir(proj, root)
        target_entries = detect_targets(os.path.join(root, path), releasable_config_dir=rel_dir)
        target_names = [e.name for e in target_entries]

        # Read version (use first target -- one version per project)
        version = "?"
        if target_entries and target_entries[0].name in TARGETS:
            try:
                version = TARGETS[target_entries[0].name].read_version(target_entries[0].path)
            except Exception:
                version = "?"

        deps = graph.dependencies(name)
        dep_names = [d.name for d in deps]
        rdep_names = graph.dependents(name)

        # Raw facts from project config
        dev_only = bool(proj.dev_only)
        library = bool(proj.get("library", False))

        # Check if any reverse-dependent has runtime or explicit scope
        rdeps_with_scope = graph._rdeps.get(name, [])
        has_runtime_dependents = any(
            scope in ("runtime", "explicit") for _, scope in rdeps_with_scope
        )
        is_leaf = len(rdeps_with_scope) == 0

        packages[name] = {
            "deps": dep_names,
            "rdeps": rdep_names,
            "targets": target_names,
            "version": version,
            "dev_only": dev_only,
            "library": library,
            "has_runtime_dependents": has_runtime_dependents,
            "is_leaf": is_leaf,
        }

        for dep in deps:
            edges.append({
                "from": name,
                "to": dep.name,
                "type": dep.dep_type,
                "constraint": dep.constraint,
                "scope": dep.scope,
            })

    return packages, edges


def _filter_packages(graph, packages, edges, root_pkg=None, reverse_pkg=None, depth=None):
    """Apply --root or --reverse filtering to the graph data.

    Returns (filtered_packages, filtered_edges).
    """
    if root_pkg is not None:
        if root_pkg not in packages:
            print(f"Error: package '{root_pkg}' not found in workspace.", file=sys.stderr)
            sys.exit(1)
        keep = set(graph.transitive_deps(root_pkg, depth=depth))
        keep.add(root_pkg)
    elif reverse_pkg is not None:
        if reverse_pkg not in packages:
            print(f"Error: package '{reverse_pkg}' not found in workspace.", file=sys.stderr)
            sys.exit(1)
        keep = set(graph.transitive_rdeps(reverse_pkg, depth=depth))
        keep.add(reverse_pkg)
    else:
        keep = set(packages.keys())

    filtered_packages = {k: v for k, v in packages.items() if k in keep}
    # Also filter deps/rdeps lists within each package entry
    for name, pkg in filtered_packages.items():
        pkg["deps"] = [d for d in pkg["deps"] if d in keep]
        pkg["rdeps"] = [r for r in pkg["rdeps"] if r in keep]

    filtered_edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return filtered_packages, filtered_edges


def _render_json(packages, edges):
    """Render the graph as JSON."""
    data = {"packages": packages, "edges": edges}
    return json.dumps(data, indent=2)


def _render_dot(packages, edges):
    """Render the graph as Graphviz DOT with scope-styled edges and fact-styled nodes."""
    _EDGE_STYLES = {
        "runtime": "",
        "dev": " [style=dashed, color=gray]",
        "peer": " [style=dotted, color=blue]",
        "explicit": " [color=black, penwidth=2]",
    }
    lines = [
        "digraph dependencies {",
        '    rankdir=TB;',
        '    node [shape=box, fontname="Helvetica", fontsize=10];',
    ]

    # Node styling based on raw facts
    for name, pkg in sorted(packages.items()):
        if pkg.get("dev_only"):
            lines.append(f'    "{name}" [style=filled, fillcolor=lightgray];')
        elif pkg.get("is_leaf"):
            lines.append(f'    "{name}" [style=filled, fillcolor=lightgreen];')

    # Edge styling based on scope
    for edge in edges:
        attrs = _EDGE_STYLES.get(edge.get("scope", "runtime"), "")
        lines.append(f'    "{edge["from"]}" -> "{edge["to"]}"{attrs};')

    lines.append("}")
    return "\n".join(lines)


def _render_text(packages, edges):
    """Render the graph as an indented text tree with fact labels."""
    lines = []
    for name in sorted(packages.keys()):
        lines.append(_text_label(name, packages.get(name, {})))
        for dep in sorted(packages[name]["deps"]):
            _render_text_subtree(dep, packages, lines, indent=1, visited={name})
    return "\n".join(lines)


def _text_label(name, pkg):
    """Build a text label with fact annotations like [dev], [lib], [leaf]."""
    labels = []
    if pkg.get("dev_only"):
        labels.append("[dev]")
    if pkg.get("library"):
        labels.append("[lib]")
    if pkg.get("is_leaf"):
        labels.append("[leaf]")
    if labels:
        return f"{name} {' '.join(labels)}"
    return name


def _render_text_subtree(name, packages, lines, indent, visited):
    """Recursively render a package and its deps as indented text."""
    label = _text_label(name, packages.get(name, {}))
    lines.append("  " * indent + label)
    if name in visited or name not in packages:
        return
    visited = visited | {name}
    for dep in sorted(packages[name]["deps"]):
        _render_text_subtree(dep, packages, lines, indent + 1, visited)


def _cmd_graph(flags, project_root):
    """Export the monorepo dependency graph."""
    start = str(project_root)
    root = find_workspace_root(start)
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    if not projects:
        print("No projects in workspace.")
        return

    graph = WorkspaceGraph(root, projects)

    packages, edges = _collect_graph_data(root, projects, graph)

    root_pkg = flags.get("root") or None
    reverse_pkg = flags.get("reverse") or None
    depth_raw = flags.get("depth")
    depth = int(depth_raw) if depth_raw is not None else None

    packages, edges = _filter_packages(
        graph, packages, edges,
        root_pkg=root_pkg, reverse_pkg=reverse_pkg, depth=depth,
    )

    fmt = flags.get("format", "json")
    if fmt == "json":
        output = _render_json(packages, edges)
    elif fmt == "dot":
        output = _render_dot(packages, edges)
    elif fmt == "text":
        output = _render_text(packages, edges)
    else:
        print(f"Error: unknown format '{fmt}'. Use json, dot, or text.", file=sys.stderr)
        sys.exit(1)

    output_file = flags.get("output") or None
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
            f.write("\n")
        print(f"Wrote graph to {output_file}")
    else:
        print(output)
