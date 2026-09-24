"""Edit-release command that updates existing GitHub Release notes by extracting the matching version section from CHANGELOG.md."""

import os
import sys

from ..member_context import resolve_member_context
from ..targets import TARGETS, resolve_releasable_config_dir
from ..utils import check_gh_auth, check_gh_installed, extract_changelog_entry, run_gh
from ..workspace import find_workspace_root, resolve_project
from ..release_publication import (
    edit_notes_args,
    notes_file,
    read_release_body,
    resynced_body,
)


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
    releasable_config_dir = None
    start_path = str(project_root)
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]
            is_non_releasable = not project.is_releasable
            releasable_config_dir = resolve_releasable_config_dir(project, monorepo_root)

            # Resolve the releasable this member belongs to.
            from ..workspace import load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
            ws_projects = _load_ws(monorepo_root)
            releasables = load_releasables(monorepo_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)
            if rel:
                releasable_name = rel.name
                releasable_tag_fmt = rel.effective_tag_format

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

    # Detect primary target (with releasable-level config inheritance)
    member = resolve_member_context(
        project_dir, releasable_config_dir=releasable_config_dir,
    )
    entries = member.targets
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

    # Extract release notes from CHANGELOG.md. For explicit-mode releasables
    # the canonical changelog lives in the releasable state dir, not the
    # member project directory.
    from ..changelog.home import get_changelog_home
    changelog_path = get_changelog_home(project_dir, releasable_dir=releasable_config_dir)
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

    # Read the existing Release's body: its absence means there is no Release
    # to edit, and its rlsbl-ci-sha marker must be kept on the re-synced notes.
    try:
        current_body = read_release_body(tag, gh=run_gh)
    except Exception:
        print(f"Error: GitHub Release for {tag} not found.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Would update GitHub Release notes for {tag}")
        print(f"Changelog entry:\n{changelog_entry}")
        return

    # The body is composed by rlsbl.release_publication, the one authority for
    # what a Release document carries, so the notes match what the release
    # flow wrote and the marker the publish check reads is kept.
    body = resynced_body(current_body, tag=tag, version=version,
                         notes=changelog_entry)
    with notes_file(body) as path:
        run_gh(edit_notes_args(tag, path))

    print(f"Updated GitHub Release notes for {tag}")
