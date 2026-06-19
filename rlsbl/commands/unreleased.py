"""Unreleased command that lists commits since the last tag and checks whether each one is covered by a corresponding changelog entry."""

import json
import subprocess
import sys

from ..changelog import changes_dir_exists, get_changes_dir, read_unreleased, resolve_hashes
from ..git_util import filter_commits_for_project
from ..targets import TARGETS, detect_targets
from ..utils import get_last_version_tag
from ..workspace import find_workspace_root, load_workspace, resolve_project


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
                # Check for explicit releasable mode
                from ..workspace import is_explicit_mode, load_releasables, resolve_releasable_for_project
                if is_explicit_mode(ws_root):
                    ws_projects = load_workspace(ws_root)
                    releasables = load_releasables(ws_root, ws_projects)
                    rel = resolve_releasable_for_project(monorepo_project, releasables)
                    if rel:
                        from ..commands.release.validate import _releasable_tag_glob
                        tag_glob = _releasable_tag_glob(rel.tag_format, rel.name)
                if tag_glob is None:
                    targets = detect_targets(root_str)
                    if targets:
                        target = TARGETS[targets[0].name]
                        tag_glob = target.monorepo_tag_glob(
                            monorepo_project["name"],
                            path=monorepo_project["path"],
                        )
    except Exception:
        pass

    tag = get_last_version_tag(tag_glob) if tag_glob else get_last_version_tag()
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

    # Non-releasable projects don't use changelogs
    is_non_releasable = monorepo_project is not None and not monorepo_project.is_releasable
    if is_non_releasable:
        if flags.get("json"):
            print(json.dumps({"tag": tag, "commits": len(commits), "non_releasable": True, "dev_only": monorepo_project.dev_only}))
        else:
            print(f"non-releasable -- no changelog ({len(commits)} unreleased commits)")
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
