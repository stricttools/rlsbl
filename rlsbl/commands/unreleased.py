"""Unreleased command that lists commits since the last tag and checks whether each one is covered by a corresponding changelog entry."""

import json
import subprocess
import sys

from ..changelog import changes_dir_exists, get_changes_dir, read_unreleased, resolve_hashes
from ..git_util import filter_commits_for_project
from ..targets import TARGETS, detect_targets
from ..workspace import find_workspace_root, load_workspace, resolve_project


def _get_last_tag(tag_glob=None):
    """Get the most recent tag. Returns None if no tags exist.

    When tag_glob is set (monorepo mode), only tags matching the glob
    are considered, so each project resolves its own last release tag.
    """
    try:
        cmd = ["git", "describe", "--tags", "--abbrev=0"]
        if tag_glob:
            cmd.extend(["--match", tag_glob])
        result = subprocess.run(
            cmd,
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



def run_cmd(registry, args, flags, project_root):
    """List unreleased commits and their changelog coverage.

    Usage: rlsbl unreleased [--json]
    """
    root_str = str(project_root)

    # Detect monorepo context for scoped tags and directory filtering
    monorepo_project = None
    tag_glob = None
    try:
        ws_root = find_workspace_root(root_str)
        if ws_root is not None:
            monorepo_project = resolve_project(ws_root, root_str)
            if monorepo_project:
                targets = detect_targets(root_str)
                if targets:
                    target = TARGETS[targets[0].name]
                    tag_glob = target.monorepo_tag_glob(
                        monorepo_project["name"],
                        path=monorepo_project["path"],
                    )
    except Exception:
        pass

    tag = _get_last_tag(tag_glob=tag_glob)
    commits = _get_commits_since(tag)

    # In monorepo mode, filter to commits touching this project's files
    if monorepo_project and commits:
        commit_shas = set(c["hash"] for c in commits)
        filtered = filter_commits_for_project(commit_shas, monorepo_project)
        commits = [c for c in commits if c["hash"] in filtered]

    if not commits:
        if flags.get("json"):
            print(json.dumps({"tag": tag, "commits": [], "coverage": {"covered": 0, "total": 0}}))
        else:
            print("No unreleased commits.")
        sys.exit(0)

    # Dev node projects don't use changelogs
    is_dev_node = monorepo_project is not None and monorepo_project.get("dev_node")
    if is_dev_node:
        if flags.get("json"):
            print(json.dumps({"tag": tag, "commits": len(commits), "dev_node": True}))
        else:
            print(f"dev node -- no changelog ({len(commits)} unreleased commits)")
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
            subject = commit["subject"]
            print(f"  {short_hash}  {subject}  {status}")

        print(f"\nCoverage: {covered_count}/{total} commits have changelog entries.")

    sys.exit(0)
