"""Yank command that marks a past release as deprecated, either soft (flag as pre-release and prepend a deprecation notice) or hard (delete the GitHub release while preserving the git tag)."""

import os
import sys
import time

from ..targets import TARGETS, detect_targets
from ..utils import run, gh_env, check_gh_installed, check_gh_auth
from ..workspace import find_workspace_root, resolve_project


def run_cmd(args, flags, project_root):
    """Yank a past GitHub Release.

    Default (soft): mark as pre-release and prepend a deprecation notice.
    With --hard: delete the GitHub Release (git tag is preserved).

    In monorepo mode, uses the project's monorepo tag format (e.g.
    ``mylib@v1.2.3``) instead of the plain ``v1.2.3`` tag.

    Args:
        args: Positional args; first element is the version to yank.
        flags: dict with keys ``dry-run``, ``yes``, ``hard``, ``reason``, ``use``.
        project_root: Path to the project root directory, or None for cwd.
    """
    dry_run = flags.get("dry-run", False)
    hard = flags.get("hard", False)
    reason = flags.get("reason")
    use = flags.get("use")

    if not args:
        print("Error: version argument is required.", file=sys.stderr)
        sys.exit(1)

    # Normalize version: strip leading "v" for display
    raw_version = args[0]
    version = raw_version.lstrip("v")

    # Detect monorepo context and build tag accordingly
    monorepo_name = None
    monorepo_project_path = None
    releasable_name = None
    releasable_tag_fmt = None
    start_path = str(project_root)
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            monorepo_name = project["name"]
            monorepo_project_path = project["path"]

            # Detect explicit releasable mode
            from ..workspace import is_explicit_mode, load_releasables, load_workspace as _load_ws, resolve_releasable_for_project
            if is_explicit_mode(monorepo_root):
                ws_projects = _load_ws(monorepo_root)
                releasables = load_releasables(monorepo_root, ws_projects)
                rel = resolve_releasable_for_project(project, releasables)
                if rel:
                    releasable_name = rel.name
                    releasable_tag_fmt = rel.tag_format

    # Project directory: project_root is already resolved to the sub-project
    # in monorepo mode (via _require_sub_project_root).
    project_dir = start_path
    entries = detect_targets(project_dir)
    if entries:
        target = TARGETS[entries[0].name]
        if releasable_name and releasable_tag_fmt:
            from .release.validate import _format_releasable_tag
            tag = _format_releasable_tag(releasable_tag_fmt, releasable_name, version)
        elif monorepo_name:
            tag = target.monorepo_tag_format(monorepo_name, version, path=monorepo_project_path)
        else:
            tag = target.tag_format(version)
    else:
        tag = f"v{version}"

    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    # Verify the GitHub Release exists
    try:
        run("gh", ["release", "view", tag], env=gh_env())
    except Exception:
        print(f"Error: GitHub Release for {tag} not found.", file=sys.stderr)
        sys.exit(1)

    # Refuse to yank the latest release -- suggest rlsbl release undo instead
    try:
        latest_line = run("gh", ["release", "list", "--limit", "1", "--json", "tagName", "--jq", ".[0].tagName"], env=gh_env())
        if latest_line == tag:
            print(
                f"Error: {tag} is the latest release. Use 'rlsbl release undo' to revert it instead.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception as e:
        print(f"Error: could not determine latest release: {e}", file=sys.stderr)
        print("Cannot verify whether this is the latest release. Aborting for safety.", file=sys.stderr)
        sys.exit(1)

    # Confirmation prompt (skipped with --yes or --dry-run)
    if not dry_run and not flags.get("yes"):
        if hard:
            prompt = f"Will DELETE GitHub Release for {tag}. Continue? [y/N] "
        else:
            prompt = f"Will mark {tag} as deprecated. Continue? [y/N] "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    if hard:
        _hard_yank(tag, dry_run)
    else:
        _soft_yank(tag, reason, use, dry_run)


def _hard_yank(tag, dry_run):
    """Delete the GitHub Release and its assets. The git tag stays."""
    if dry_run:
        print(f"Would delete GitHub Release {tag}")
        return

    run("gh", ["release", "delete", tag, "--yes"], env=gh_env())
    print(f"Deleted GitHub Release {tag}")


def _soft_yank(tag, reason, use, dry_run):
    """Mark as pre-release and prepend a deprecation notice to the release body."""
    # Build deprecation notice
    notice = _build_notice(reason, use)

    # Get current release body
    try:
        current_body = run("gh", ["release", "view", tag, "--json", "body", "--jq", ".body"], env=gh_env())
    except Exception:
        current_body = ""

    new_body = notice + "\n\n" + current_body if current_body else notice

    if dry_run:
        print(f"Would mark {tag} as pre-release with deprecation notice:")
        print(notice)
        return

    # Write new body to a temp file to avoid shell escaping issues
    notes_file = f".rlsbl-yank-{int(time.time() * 1000)}.tmp"
    writing_file = notes_file + ".writing"
    try:
        with open(writing_file, "w", encoding="utf-8") as f:
            f.write(new_body)
        os.rename(writing_file, notes_file)
        run("gh", ["release", "edit", tag, "--prerelease", "--notes-file", notes_file], env=gh_env())
    finally:
        for tmp in (notes_file, writing_file):
            if os.path.exists(tmp):
                os.unlink(tmp)

    print(f"Yanked {tag} (marked as pre-release)")


def _build_notice(reason, use):
    """Build the deprecation notice string from optional reason and use fields."""
    parts = []
    if reason:
        parts.append(reason)
    if use:
        use_version = use.lstrip("v")
        parts.append(f"Use v{use_version} instead")

    if parts:
        return "> **Deprecated:** " + ". ".join(parts) + "."
    return "> **Deprecated.**"
