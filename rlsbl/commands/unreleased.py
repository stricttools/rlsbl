"""Unreleased command that lists commits since the last tag and checks whether each one is covered by a corresponding changelog entry."""

import json
import subprocess
import sys

from ..changelog import changes_dir_exists, get_changes_dir, read_unreleased, resolve_hashes


def _get_last_tag():
    """Get the most recent tag. Returns None if no tags exist."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _get_commits_since(tag):
    """Get commits since the given tag (or all commits if tag is None).

    Returns a list of dicts with keys: hash, subject, author, date.
    Uses NUL as field separator for safe parsing.
    """
    # Format: hash<NUL>subject<NUL>author<NUL>ISO-date
    fmt = "%H%x00%s%x00%an%x00%aI"
    if tag:
        range_spec = f"{tag}..HEAD"
    else:
        range_spec = "HEAD"

    try:
        result = subprocess.run(
            ["git", "log", range_spec, f"--format={fmt}"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    lines = result.stdout.strip().split("\n")
    commits = []
    for line in lines:
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) != 4:
            continue
        commits.append({
            "hash": parts[0],
            "subject": parts[1],
            "author": parts[2],
            "date": parts[3],
        })
    return commits



def run_cmd(registry, args, flags, project_root=None):
    """List unreleased commits and their changelog coverage.

    Usage: rlsbl unreleased [--json]
    """
    if project_root is None:
        project_root = "."
    root_str = str(project_root)

    tag = _get_last_tag()
    commits = _get_commits_since(tag)

    if not commits:
        if flags.get("json"):
            print(json.dumps({"tag": tag, "commits": [], "coverage": {"covered": 0, "total": 0}}))
        else:
            print("No unreleased commits.")
        sys.exit(0)

    # Cross-reference each commit against JSONL changelog
    if not changes_dir_exists(root_str):
        print(
            "Error: JSONL changelog not set up. Run 'rlsbl scaffold' to create .rlsbl/changes/",
            file=sys.stderr,
        )
        sys.exit(1)

    changes_dir = get_changes_dir(root_str)
    entries = read_unreleased(changes_dir)
    all_hashes = []
    for entry in entries:
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)
    covered_shas = {full for full in resolved.values() if full is not None}
    for commit in commits:
        commit["covered"] = commit["hash"] in covered_shas

    covered_count = sum(1 for c in commits if c["covered"])
    total = len(commits)

    if flags.get("json"):
        output = {
            "tag": tag,
            "commits": commits,
            "coverage": {"covered": covered_count, "total": total},
        }
        print(json.dumps(output, indent=2))
    else:
        tag_display = tag or "(no tags)"
        print(f"Unreleased commits since {tag_display} ({total} commits):\n")
        for commit in commits:
            short_hash = commit["hash"][:7]
            status = "[COVERED]" if commit["covered"] else "[MISSING]"
            # Truncate long subjects to keep output aligned
            subject = commit["subject"]
            if len(subject) > 50:
                subject = subject[:47] + "..."
            print(f"  {short_hash}  {subject:<50}  {status}")

        print(f"\nCoverage: {covered_count}/{total} commits have changelog entries.")

    sys.exit(0)
