"""Edit-release command that updates existing GitHub Release notes by extracting the matching version section from CHANGELOG.md."""

import os
import sys
import time

from ..targets import TARGETS, detect_targets
from ..utils import check_gh_auth, check_gh_installed, extract_changelog_entry, gh_env, run
from ..workspace import find_workspace_root, resolve_project


def run_cmd(args, flags, project_root):
    """Update GitHub Release notes from the changelog entry for a version.

    If no version is given, detects the current version from the project's
    primary target. Reads the changelog entry and updates the GitHub Release.

    In monorepo mode, uses the project's monorepo tag format and reads
    CHANGELOG.md from the project subdirectory.

    Args:
        args: Positional args; optional first element is the version.
        flags: dict with key ``dry-run``.
        project_root: Path to the project root directory, or None for cwd.
    """
    dry_run = flags.get("dry-run", False)

    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    # Detect monorepo context
    monorepo_name = None
    monorepo_project_path = None
    is_non_releasable = False
    releasable_name = None
    releasable_tag_fmt = None
    start_path = str(project_root)
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]
            is_non_releasable = not project.is_releasable

            # Detect explicit releasable mode
            from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
            if is_explicit_mode(monorepo_root):
                ws_projects = _load_ws(monorepo_root)
                releasables = load_releasables(monorepo_root, ws_projects)
                rel = resolve_releasable_for_project(project, releasables)
                if rel:
                    releasable_name = rel.name
                    releasable_tag_fmt = rel.tag_format

    if is_non_releasable:
        print(
            "Error: non-releasable projects cannot be released and have no "
            "release to edit. Set releasable = \"<name>\" in workspace.toml "
            "if this project should be releasable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Project directory: project_root is already resolved to the sub-project
    # in monorepo mode (via _require_sub_project_root).
    project_dir = start_path

    # Detect primary target
    entries = detect_targets(project_dir)
    if not entries:
        print("Error: no package.json, pyproject.toml, or go.mod found.", file=sys.stderr)
        sys.exit(1)
    primary = entries[0]
    target = TARGETS[primary.name]

    # Resolve version
    if args:
        raw_version = args[0]
    else:
        raw_version = target.read_version(primary.path)

    # Normalize: strip leading "v" for changelog lookup
    version = raw_version.lstrip("v")

    # Build the tag: releasable format, monorepo format, or standalone
    if releasable_name and releasable_tag_fmt:
        from .release.validate import _format_releasable_tag
        tag = _format_releasable_tag(releasable_tag_fmt, releasable_name, version)
    elif monorepo_name:
        tag = target.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
    else:
        tag = target.tag_format(version)

    # Extract release notes from CHANGELOG.md
    changelog_path = os.path.join(project_dir, "CHANGELOG.md")
    if not os.path.exists(changelog_path):
        print("Error: CHANGELOG.md not found.", file=sys.stderr)
        sys.exit(1)

    changelog_entry = extract_changelog_entry(changelog_path, version)
    if not changelog_entry:
        print(
            f"Error: no changelog entry found for version {version} in CHANGELOG.md.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check that the GitHub Release exists
    try:
        run("gh", ["release", "view", tag], env=gh_env())
    except Exception:
        print(f"Error: GitHub Release for {tag} not found.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Would update GitHub Release notes for {tag}")
        print(f"Changelog entry:\n{changelog_entry}")
        return

    # Write notes to a temp file to avoid shell escaping issues
    notes_file = f".rlsbl-notes-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(changelog_entry)
        os.rename(writing_file, notes_file)
        run("gh", ["release", "edit", tag, "--notes-file", notes_file], env=gh_env())
    finally:
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                os.unlink(tmp)

    print(f"Updated GitHub Release notes for {tag}")
