"""Yank command that marks a past release as deprecated, either soft (flag as pre-release and prepend a deprecation notice) or hard (delete the GitHub release while preserving the git tag)."""

import os
import sys
import time

from ..utils import run, check_gh_installed, check_gh_auth


def run_cmd(args, flags):
    """Yank a past GitHub Release.

    Default (soft): mark as pre-release and prepend a deprecation notice.
    With --hard: delete the GitHub Release (git tag is preserved).
    """
    dry_run = flags.get("dry-run", False)
    hard = flags.get("hard", False)
    reason = flags.get("reason")
    use = flags.get("use")

    if not args:
        print("Error: version argument is required.", file=sys.stderr)
        sys.exit(1)

    # Normalize version: strip leading "v" for display, ensure "v" prefix for tag
    raw_version = args[0]
    version = raw_version.lstrip("v")
    tag = f"v{version}"

    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated.", file=sys.stderr)
        sys.exit(1)

    # Verify the GitHub Release exists
    try:
        run("gh", ["release", "view", tag])
    except Exception:
        print(f"Error: GitHub Release for {tag} not found.", file=sys.stderr)
        sys.exit(1)

    # Refuse to yank the latest release -- suggest rlsbl undo instead
    try:
        latest_line = run("gh", ["release", "list", "--limit", "1", "--json", "tagName", "--jq", ".[0].tagName"])
        if latest_line == tag:
            print(
                f"Error: {tag} is the latest release. Use 'rlsbl undo' to revert it instead.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception:
        # If we cannot determine the latest release, proceed anyway
        pass

    if hard:
        _hard_yank(tag, dry_run)
    else:
        _soft_yank(tag, reason, use, dry_run)


def _hard_yank(tag, dry_run):
    """Delete the GitHub Release and its assets. The git tag stays."""
    if dry_run:
        print(f"Would delete GitHub Release {tag}")
        return

    run("gh", ["release", "delete", tag, "--yes"])
    print(f"Deleted GitHub Release {tag}")


def _soft_yank(tag, reason, use, dry_run):
    """Mark as pre-release and prepend a deprecation notice to the release body."""
    # Build deprecation notice
    notice = _build_notice(reason, use)

    # Get current release body
    try:
        current_body = run("gh", ["release", "view", tag, "--json", "body", "--jq", ".body"])
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
        run("gh", ["release", "edit", tag, "--prerelease", "--notes-file", notes_file])
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
