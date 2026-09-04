"""Unreleased command that lists the commits since the release the release record anchors this checkout to, and checks whether each one is covered by a corresponding changelog entry."""

import os
import subprocess
import sys

from ..changelog import read_unreleased, resolve_hashes
from ..changelog.validate import filter_exempt_commits
from ..context import resolve_release_scope
from ..git_util import filter_commits_for_scope
from ..release_record import (
    latest_release_fact,
    range_anchor,
    releases_dir_for_changes_dir,
)
from .. import effects


def _get_commits_since(anchor_sha):
    """Get commits since *anchor_sha* (or all commits when it is None).

    *anchor_sha* is the released commit the release record anchors this checkout to,
    not a tag: a range expressed as a commit resolves even where the version's
    tag was deleted or moved.

    Returns a list of dicts with keys: hash, subject, author, date.
    Uses NUL as field separator for safe parsing.
    """
    # Format: hash<NUL>subject<NUL>author<NUL>ISO-date
    fmt = "%H%x00%s%x00%an%x00%aI"
    if anchor_sha:
        range_spec = f"{anchor_sha}..HEAD"
    else:
        range_spec = "HEAD"

    try:
        result = effects.run(
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

    Returns the machine payload -- the caller (the CLI handler) hands it to the
    framework, which emits it only in machine mode.  Human output is printed
    here unless ``flags["json"]`` is set, in which case machine mode owns
    stdout and nothing human may reach it.
    """
    root_str = str(project_root)

    monorepo_project, tag_glob, changes_dir, scope = resolve_release_scope(root_str)

    release_record_dir = releases_dir_for_changes_dir(changes_dir)
    anchor = range_anchor(release_record_dir, tag_glob=tag_glob, cwd=root_str)
    # Two different questions, deliberately answered from two different
    # places: the RANGE is bounded by the highest release this checkout
    # contains, while the release the header names is the project's latest,
    # annotated when this checkout does not contain it.
    latest = latest_release_fact(release_record_dir, tag_glob=tag_glob, cwd=root_str)
    commits = _get_commits_since(anchor.candidate_sha if anchor else None)

    # Scope to the project's (or the whole releasable's) files FIRST, then
    # apply the exemption filter -- the same order the authoritative coverage
    # check uses, so `unreleased`, `status`, and `rlsbl check --tag changelog`
    # answer the same question the same way.
    if scope is not None and commits:
        commit_shas = set(c["hash"] for c in commits)
        in_scope = filter_commits_for_scope(
            commit_shas, scope, operation="unreleased listing",
        )
        commits = [c for c in commits if c["hash"] in in_scope]

    if not commits:
        if not flags.get("json"):
            print("No unreleased commits.")
        return {
            "latest_release": latest.version,
            "latest_release_in_checkout": latest.in_checkout,
            "range_anchor_version": anchor.version if anchor else None,
            "commits": [],
            "coverage": {"covered": 0, "total": 0, "exempted": 0},
        }

    # Non-releasable projects don't use changelogs
    is_non_releasable = monorepo_project is not None and not monorepo_project.is_releasable
    if is_non_releasable:
        if not flags.get("json"):
            print(f"non-releasable -- no changelog ({len(commits)} unreleased commits)")
        return {
            "latest_release": latest.version,
            "latest_release_in_checkout": latest.in_checkout,
            "range_anchor_version": anchor.version if anchor else None,
            "commits": len(commits),
            "non_releasable": True, "dev_only": monorepo_project.dev_only,
        }

    # Cross-reference each commit against JSONL changelog
    if not os.path.isdir(changes_dir):
        print(
            f"Error: JSONL changelog not set up ({changes_dir}). "
            "Run 'rlsbl scaffold' to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    entries = read_unreleased(changes_dir)
    all_hashes = []
    for entry in entries:
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)
    covered_shas = {full for full in resolved.values() if full is not None}

    # Autogenerated and changelog-only commits need no entry. Without this
    # filter `unreleased` reported them as MISSING while `status` exempted
    # them, so the two commands contradicted each other on the same repo.
    non_exempt, _exempt_stats = filter_exempt_commits([c["hash"] for c in commits])
    non_exempt_shas = set(non_exempt)
    for commit in commits:
        commit["exempt"] = commit["hash"] not in non_exempt_shas
        commit["covered"] = commit["hash"] in covered_shas

    tracked = [c for c in commits if not c["exempt"]]
    covered_count = sum(1 for c in tracked if c["covered"])
    total = len(tracked)
    exempted = len(commits) - total

    if not flags.get("json"):
        since = anchor.version if anchor else "(no release in this history)"
        print(f"Unreleased commits since {since} ({len(commits)} commits):\n")
        if latest.version is not None and latest.in_checkout is False:
            print(f"latest release: {latest.label()}\n")
        for commit in commits:
            short_hash = commit["hash"][:7]
            if commit["exempt"]:
                status = "[EXEMPT]"
            elif commit["covered"]:
                status = "[COVERED]"
            else:
                status = "[MISSING]"
            subject = commit["subject"]
            print(f"  {short_hash}  {subject}  {status}")

        suffix = f" ({exempted} exempted)" if exempted else ""
        print(f"\nCoverage: {covered_count}/{total} commits covered{suffix}.")

    return {
        "latest_release": latest.version,
        "latest_release_in_checkout": latest.in_checkout,
        "range_anchor_version": anchor.version if anchor else None,
        "commits": commits,
        "coverage": {
            "covered": covered_count, "total": total, "exempted": exempted,
        },
    }
