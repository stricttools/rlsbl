"""release init command: scaffold a release file by auto-detecting project targets."""

import os
import sys


def run_cmd(project_root):
    """Create .rlsbl/releases/unreleased.toml with auto-detected targets.

    Args:
        project_root: Path to the project root directory.
    """
    import tomlkit

    from ..release_file import get_release_file_path
    from ..targets import detect_targets
    from ..workspace import find_workspace_root, resolve_project

    # In monorepo mode, create the release file in the package's directory
    start_path = str(project_root)
    project_dir = start_path
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        from ..workspace import is_explicit_mode
        if is_explicit_mode(monorepo_root):
            print(
                "Warning: this project belongs to a monorepo workspace that uses "
                "[[releasables]] (explicit mode). Batch releases should use "
                "'rlsbl monorepo release-init' instead of 'rlsbl release init'.",
                file=sys.stderr,
            )
        project = resolve_project(monorepo_root, start_path)
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
    doc.add(tomlkit.comment("Version bump type: patch, minor, or major"))
    doc.add("bump", "")
    doc.add(tomlkit.comment("Short description of this release (required)"))
    doc.add("description", "")
    doc.add(tomlkit.comment("Optional context explaining why these changes were made"))
    doc.add("context", "")
    doc.add(tomlkit.comment("Set to true to generate a blog post for this release"))
    doc.add(tomlkit.comment("blog = false"))
    doc.add("include", target_names)
    doc.add("exclude", [])

    # Add per-target config sections for Flutter target
    flutter_targets = [n for n in target_names if n == "flutter"]
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
