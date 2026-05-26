"""release init command: scaffold a release file by auto-detecting project targets."""

import os
import sys


def run_cmd():
    """Create .rlsbl/releases/unreleased.toml with auto-detected targets."""
    import tomlkit

    from ..release_file import get_release_file_path
    from ..targets import detect_targets
    from ..workspace import find_workspace_root, resolve_project

    # In monorepo mode, create the release file in the package's directory
    project_dir = "."
    monorepo_root = find_workspace_root(".")
    if monorepo_root:
        project = resolve_project(monorepo_root, ".")
        if project is not None:
            project_dir = os.path.join(monorepo_root, project["path"])

    release_path = get_release_file_path(project_dir)
    if os.path.exists(release_path):
        content = open(release_path).read().strip()
        if content:
            print(f"Error: {release_path} already exists.", file=sys.stderr)
            sys.exit(1)

    entries = detect_targets(project_dir)
    if not entries:
        print("Error: no targets detected in the current directory.", file=sys.stderr)
        sys.exit(1)

    target_names = [e.name for e in entries]

    doc = tomlkit.document()
    doc.add("bump", "patch")
    doc.add("include", target_names)
    doc.add("exclude", [])

    # Add per-target config sections for Flutter targets
    flutter_targets = [n for n in target_names if "flutter" in n]
    if flutter_targets:
        targets_table = tomlkit.table(is_super_table=True)
        for ft in flutter_targets:
            t = tomlkit.table()
            t.add("mode", "build")
            targets_table.add(ft, t)
        doc.add("targets", targets_table)

    releases_dir = os.path.dirname(release_path)
    os.makedirs(releases_dir, exist_ok=True)

    with open(release_path, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)

    print(release_path)
