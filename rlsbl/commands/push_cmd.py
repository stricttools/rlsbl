"""rlsbl push: tool-mediated push for non-release (dev) branches.

Provides branch guard (refuses release branches), preflight changelog
coverage with actionable remediation hints, behind-remote refusal, and
a wrapped ``git push``. The pre-push hook still runs as a backstop.
"""

import os
import subprocess
import sys

from ..errors import ConfigError, GitError
from ..prepush_utils import (
    _check_jsonl_changelog,
    _get_pushed_commits,
    _get_release_branches,
)
from ..utils import get_current_branch, get_push_timeout, remote_branch_exists, run


def _check_branch_guard(branch, release_branches):
    """Hard-error if branch is a release branch.

    Returns an error message string, or None if the branch is allowed.
    """
    if branch in release_branches:
        return (
            f"Cannot push to release branch '{branch}'; "
            f"use `rlsbl release run` instead."
        )
    return None


def _check_behind_remote(branch):
    """Fetch origin and check if local branch is behind remote.

    Returns an error message string, or None if the branch is not behind.
    """
    # Fetch the remote tracking branch
    try:
        subprocess.run(
            ["git", "fetch", "origin", branch],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Fetch failed -- cannot determine behind status, skip
        return None

    if not remote_branch_exists(branch):
        # New branch, not behind anything
        return None

    # Count commits the local branch is behind
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{branch}..origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        behind_count = int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None

    if behind_count > 0:
        return (
            f"your branch is {behind_count} commit(s) behind "
            f"origin/{branch}; pull/rebase first."
        )
    return None


def _build_coverage_refs(branch):
    """Build (local_sha, remote_sha) ref pairs for coverage checking.

    Mimics the ref format the pre-push hook receives, so we can reuse
    ``_get_pushed_commits`` and ``_check_jsonl_changelog`` directly.
    """
    zero_sha = "0" * 40
    local_sha = run("git", ["rev-parse", branch])
    if remote_branch_exists(branch):
        remote_sha = run("git", ["rev-parse", f"origin/{branch}"])
    else:
        remote_sha = zero_sha
    return [(local_sha, remote_sha)]


def _check_orphaned_entries(dir_path, changes_dir=None):
    """Check for stale/orphaned entries in unreleased.jsonl.

    Returns a list of error strings (empty if no orphans found).
    """
    from ..changelog import get_changes_dir, read_unreleased
    from ..changelog.resolve import resolve_hashes

    if changes_dir is None:
        changes_dir = get_changes_dir(dir_path)
    entries = read_unreleased(changes_dir)
    if not entries:
        return []

    orphan_errors = []
    for i, entry in enumerate(entries):
        if not entry.commits:
            continue
        resolved = resolve_hashes(entry.commits)
        n_unresolvable = sum(1 for v in resolved.values() if v is None)
        if n_unresolvable == len(entry.commits):
            stale_list = ", ".join(entry.commits)
            orphan_errors.append(
                f"entry {i + 1}: all commits are stale "
                f"({stale_list}) -- run `rlsbl changelog remap`"
            )
    return orphan_errors


def _format_uncovered_hint(error_msg):
    """Transform the raw coverage error into an actionable remediation hint.

    The raw message from _check_jsonl_changelog looks like:
        "JSONL changelog missing coverage for N commit(s): sha1, sha2"

    We reformat to include the remediation command.
    """
    # Extract the commit SHAs from the error
    if "missing coverage for" in error_msg:
        # Parse out the SHAs after the colon
        parts = error_msg.split(": ", 1)
        if len(parts) == 2:
            shas = parts[1].strip()
            sha_list = [s.strip() for s in shas.split(",")]
            hint_lines = [f"commits {shas} are uncovered"]
            for sha in sha_list:
                hint_lines.append(
                    f"  run: rlsbl changelog add --commits {sha}"
                )
            return "\n".join(hint_lines)
    return error_msg


def run_push(ctx, *, yes, quiet):
    """Execute the push command.

    Steps:
    1. Branch guard: refuse release branches.
    2. Preflight coverage check with remediation hints.
    3. Behind-remote refusal.
    4. Execute ``git push`` (without RLSBL_RELEASE_PUSH).
    """
    branch = get_current_branch()

    # 1. Branch guard
    release_branches = _get_release_branches(ctx)
    branch_error = _check_branch_guard(branch, release_branches)
    if branch_error:
        print(f"Error: {branch_error}", file=sys.stderr)
        sys.exit(1)

    # 2. Preflight coverage check (before git push, with better errors)
    from ..changelog import changes_dir_exists

    dir_path = str(ctx.project_root)
    if changes_dir_exists(dir_path):
        refs = _build_coverage_refs(branch)

        # Check for orphaned/stale entries first
        orphan_errors = _check_orphaned_entries(dir_path)
        if orphan_errors:
            print(
                "Error: changelog entries reference rebased-away commits:",
                file=sys.stderr,
            )
            for err in orphan_errors:
                print(f"  {err}", file=sys.stderr)
            sys.exit(1)

        # Check coverage
        coverage_error = _check_jsonl_changelog(dir_path, refs)
        if coverage_error:
            hint = _format_uncovered_hint(coverage_error)
            print(f"Error: {hint}", file=sys.stderr)
            sys.exit(1)

    # 3. Behind-remote refusal
    behind_error = _check_behind_remote(branch)
    if behind_error:
        print(f"Error: {behind_error}", file=sys.stderr)
        sys.exit(1)

    # 4. Confirmation
    if not yes:
        print(f"Push branch '{branch}' to origin? [y/N] ", end="", flush=True)
        answer = input().strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    # 5. Execute git push (no RLSBL_RELEASE_PUSH -- this is a dev push)
    timeout = get_push_timeout(ctx.config)
    if not quiet:
        print(f"Pushing {branch} to origin...")

    try:
        push_cmd = ["git", "push", "origin", branch]
        if not remote_branch_exists(branch):
            push_cmd = ["git", "push", "-u", "origin", branch]
        result = subprocess.run(
            push_cmd,
            timeout=timeout,
            text=True,
        )
        if result.returncode != 0:
            print("Error: git push failed", file=sys.stderr)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print(
            f"Error: push timed out after {timeout}s -- "
            f"remote state may be inconsistent",
            file=sys.stderr,
        )
        sys.exit(1)

    if not quiet:
        print("Push complete.")
