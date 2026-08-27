"""Unreleased command that lists commits since the last tag and checks whether each one is covered by a corresponding changelog entry."""

import os
import subprocess
import sys

from ..changelog import get_changes_dir, read_unreleased, resolve_hashes
from ..changelog.validate import filter_exempt_commits
from ..git_util import filter_commits_for_scope
from ..targets import TARGETS, detect_targets
from ..utils import get_last_version_tag
from ..workspace import find_workspace_root, load_workspace, resolve_project
from .. import effects


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


def _resolve_scope(root_str):
    """Resolve the monorepo/releasable context for the project at *root_str*.

    Returns ``(project, tag_glob, changes_dir, members)``:

    - ``project`` is the WorkspaceProject (None in standalone mode).
    - ``tag_glob`` scopes the "last release tag" lookup.
    - ``changes_dir`` is the releasable-aware JSONL directory. A releasable
      member's entries live under ``.rlsbl-monorepo/releasables/<name>/``,
      NOT under the member package -- resolving it per-package made every
      releasable member report "JSONL changelog not set up".
    - ``scope`` is the ownership scope commits are attributed against: the
      whole releasable when the project is in one, the single member
      otherwise, and ``None`` outside a workspace.
    """
    project = None
    tag_glob = None
    changes_dir = get_changes_dir(root_str)
    scope = None
    try:
        ws_root = find_workspace_root(root_str)
        if ws_root is None:
            return project, tag_glob, changes_dir, scope
        project = resolve_project(ws_root, root_str)
        if project is None:
            return project, tag_glob, changes_dir, scope

        from ..workspace import (
            get_releasable_changes_dir,
            is_explicit_mode,
            load_releasables,
            members_of,
            resolve_releasable_for_project,
        )

        from ..ownership import OwnershipScope

        rel = None
        ws_projects = load_workspace(ws_root)
        if is_explicit_mode(ws_root):
            releasables = load_releasables(ws_root, ws_projects)
            rel = resolve_releasable_for_project(project, releasables)

        if rel is not None:
            from ..commands.release.validate import _releasable_tag_glob
            tag_glob = _releasable_tag_glob(rel.tag_format, rel.name)
            changes_dir = get_releasable_changes_dir(ws_root, rel.name)
            scope = OwnershipScope.for_members(
                ws_projects, members_of(rel.name, ws_projects),
            )
        else:
            scope = OwnershipScope.for_member(ws_projects, project)
            from ..targets import resolve_releasable_config_dir
            rel_dir = resolve_releasable_config_dir(project, ws_root)
            targets = detect_targets(root_str, releasable_config_dir=rel_dir)
            if targets:
                target = TARGETS[targets[0].name]
                tag_glob = target.monorepo_tag_glob(
                    project["name"], path=project["path"],
                )
    except Exception:
        pass
    return project, tag_glob, changes_dir, scope


def run_cmd(registry, args, flags, project_root):
    """List unreleased commits and their changelog coverage.

    Returns the machine payload -- the caller (the CLI handler) hands it to the
    framework, which emits it only in machine mode.  Human output is printed
    here unless ``flags["json"]`` is set, in which case machine mode owns
    stdout and nothing human may reach it.
    """
    root_str = str(project_root)

    monorepo_project, tag_glob, changes_dir, scope = _resolve_scope(root_str)

    tag = get_last_version_tag(tag_glob) if tag_glob else get_last_version_tag()
    commits = _get_commits_since(tag)

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
            "tag": tag, "commits": [],
            "coverage": {"covered": 0, "total": 0, "exempted": 0},
        }

    # Non-releasable projects don't use changelogs
    is_non_releasable = monorepo_project is not None and not monorepo_project.is_releasable
    if is_non_releasable:
        if not flags.get("json"):
            print(f"non-releasable -- no changelog ({len(commits)} unreleased commits)")
        return {
            "tag": tag, "commits": len(commits),
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
        tag_display = tag or "(no tags)"
        print(f"Unreleased commits since {tag_display} ({len(commits)} commits):\n")
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
        "tag": tag,
        "commits": commits,
        "coverage": {
            "covered": covered_count, "total": total, "exempted": exempted,
        },
    }
