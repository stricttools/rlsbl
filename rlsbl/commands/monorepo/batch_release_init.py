"""Batch release init command: scaffold a batch release file for all workspace projects."""

import os
import sys

import tomlkit

from ...release_file import get_batch_release_file_path
from ...targets import detect_targets
from ...workspace import find_workspace_root, load_workspace


def _cmd_batch_release_init(project_root):
    """Create .rlsbl-monorepo/releases/unreleased.toml with per-package sections.

    Iterates over all workspace projects (skipping dev_node projects),
    detects targets for each, and scaffolds a [packages.<name>] section
    with empty bump/description and the detected include list.

    Args:
        project_root: Path to the project root directory.
    """
    start = str(project_root)
    workspace_root = find_workspace_root(start)
    if workspace_root is None:
        print(
            "Error: No workspace found. Run 'rlsbl monorepo init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    batch_path = get_batch_release_file_path(workspace_root)
    if os.path.exists(batch_path):
        content = open(batch_path).read().strip()
        if content:
            print(f"Error: {batch_path} already exists.", file=sys.stderr)
            sys.exit(1)

    projects = load_workspace(workspace_root)
    if not projects:
        print("Error: no projects in workspace.", file=sys.stderr)
        sys.exit(1)

    doc = tomlkit.document()
    packages = tomlkit.table(is_super_table=True)

    any_added = False
    for proj in projects:
        if proj.get("dev_node", False):
            print(f"Skipping dev_node project: {proj['name']}", file=sys.stderr)
            continue

        project_dir = os.path.join(workspace_root, proj["path"])
        entries = detect_targets(project_dir)
        if not entries:
            print(
                f"Warning: no targets detected for {proj['name']}, skipping.",
                file=sys.stderr,
            )
            continue

        target_names = [e.name for e in entries]

        pkg_table = tomlkit.table()
        pkg_table.add(tomlkit.comment("Version bump type: patch, minor, or major"))
        pkg_table.add("bump", "")
        pkg_table.add(tomlkit.comment("Short description of this release (required)"))
        pkg_table.add("description", "")
        pkg_table.add(tomlkit.comment("Optional context explaining why these changes were made"))
        pkg_table.add("context", "")
        pkg_table.add("include", target_names)
        pkg_table.add("exclude", [])

        # Add per-target config sections for Flutter targets
        flutter_targets = [n for n in target_names if "flutter" in n]
        if flutter_targets:
            targets_table = tomlkit.table(is_super_table=True)
            for ft in flutter_targets:
                t = tomlkit.table()
                t.add("mode", "build")
                targets_table.add(ft, t)
            pkg_table.add("targets", targets_table)

        packages.add(proj["name"], pkg_table)
        any_added = True

    if not any_added:
        print("Error: no eligible projects with detected targets.", file=sys.stderr)
        sys.exit(1)

    doc.add("packages", packages)

    releases_dir = os.path.dirname(batch_path)
    os.makedirs(releases_dir, exist_ok=True)

    with open(batch_path, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)

    print(batch_path)
