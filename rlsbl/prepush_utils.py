"""Pre-push check utilities shared between checks/prepush.py and checks/workspace.py, providing commit enumeration and JSONL coverage verification.

Extracted from commands/pre_push_check.py to break the checks/ -> commands/
circular dependency. The original module existed solely to host these
functions after the pre-push-check CLI command was removed.
"""

import os
import subprocess
import sys

from .changelog import get_changes_dir, list_versioned_files, parse_jsonl, read_unreleased, resolve_hashes
from .errors import ConfigError
from .changelog.validate import filter_exempt_commits


DEFAULT_RELEASE_BRANCHES = ["main", "master"]


def _get_release_branches(ctx):
    """Return the configured release-branch list.

    Reads ``release_branches`` from ``.rlsbl/config.json`` if present;
    falls back to ``["main", "master"]`` when the key is absent.

    Branch-role semantics:

    - Branches **in** this list are **release-only**: manual pushes
      trigger a warning (``prepush-manual-warning``), and
      ``rlsbl release run`` targets them as the release branch.
    - Everything else is a **shareable** (dev) branch: sessions push
      freely; ``rlsbl release run`` from a dev branch fast-forward
      merges into the first release branch before releasing.

    Changelog coverage (``prepush-changelog-coverage``) is enforced
    on every push regardless of target branch -- dev branches are not
    exempt.

    In monorepos the dev branch is workspace-global (no per-project
    dev branches).

    Raises :class:`ConfigError` if the key is present but malformed
    (empty list or non-list value). An empty list would silently
    disable the manual-release-push warning, which is almost never
    what the user wants; require explicit removal of the key instead.
    """
    config = ctx.config
    if "release_branches" not in config:
        return list(DEFAULT_RELEASE_BRANCHES)
    branches = config["release_branches"]
    if not isinstance(branches, list):
        raise ConfigError(
            "release_branches in .rlsbl/config.json must be a list of "
            f"branch names; got {type(branches).__name__}. "
            "Remove the key to use the default (main, master)."
        )
    if not branches:
        raise ConfigError(
            "release_branches in .rlsbl/config.json is an empty list, "
            "which is not a valid value (it would disable the "
            "manual-release-push warning entirely). "
            "Remove the key to use the default (main, master), or list "
            "at least one branch name."
        )
    return [str(b) for b in branches]


def _check_gitignore_guard(dir_path, *, extra_paths=None):
    """Check that rlsbl-managed files are not gitignored.

    When extra_paths is provided, those paths are also checked (e.g.,
    releasable-level changes files in explicit monorepo mode).

    Returns an error string listing gitignored paths, or None if all clear.
    """
    paths = [
        os.path.join(dir_path, ".rlsbl", "changes", "unreleased.jsonl"),
        os.path.join(dir_path, ".rlsbl", "changes", ".validated"),
        os.path.join(dir_path, "CHANGELOG.md"),
    ]
    if extra_paths:
        paths.extend(extra_paths)
    gitignored = []
    for path in paths:
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                gitignored.append(path)
            elif result.returncode == 128:
                # Not in a git repo -- skip gracefully
                return None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
    if gitignored:
        listed = ", ".join(gitignored)
        return f"rlsbl-managed files are gitignored (must be tracked): {listed}"
    return None


def _check_jsonl_changelog(dir_path, refs, pushed_commits=None, *, changes_dir=None):
    """Check JSONL changelog coverage for commits being pushed.

    Verifies that every commit in the push range exists in at least one
    unreleased.jsonl entry's commits list.

    When pushed_commits is provided, uses that set directly instead of
    calling _get_pushed_commits(refs). This allows callers (e.g. monorepo
    check) to pass a pre-filtered set of commits.

    When changes_dir is provided, uses that directory instead of computing
    it from dir_path. This supports explicit releasable mode where the
    changes directory lives at the releasable level, not per-project.

    Returns None on success, or an error message string on failure.
    """
    if changes_dir is None:
        changes_dir = get_changes_dir(dir_path)
    entries = read_unreleased(changes_dir)

    # Also read all versioned JSONL files (e.g. 0.30.1.jsonl) so that
    # entries finalized by `rlsbl release` still provide coverage.
    for _version, filepath in list_versioned_files(changes_dir):
        entries.extend(parse_jsonl(filepath))

    # Get pushed commits from the refs, or use the provided set
    if pushed_commits is None:
        pushed_commits = _get_pushed_commits(refs)
    if pushed_commits is None:
        return None  # Could not determine pushed commits -- skip

    # Filter out autogenerated commits and changelog-only commits
    # (release infrastructure) -- these don't need JSONL coverage.
    non_exempt, _stats = filter_exempt_commits(list(pushed_commits))
    commits_needing_coverage = set(non_exempt)
    if not commits_needing_coverage:
        return None

    if not entries:
        return "no JSONL changelog entries found"

    # Collect all hashes from entries and resolve them
    all_hashes = []
    for entry in entries:
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)
    covered_shas = {full for full in resolved.values() if full is not None}

    missing = []
    for sha in commits_needing_coverage:
        if sha not in covered_shas:
            missing.append(sha[:12])

    if missing:
        return f"JSONL changelog missing coverage for {len(missing)} commit(s): {', '.join(missing)}"

    return None


def _get_pushed_commits(refs):
    """Get the set of commit SHAs being pushed.

    Returns a set of full 40-char SHAs, or None on error.
    """
    zero_sha = "0" * 40
    commits = set()

    for local_sha, remote_sha in refs:
        if local_sha == zero_sha:
            continue  # Branch deletion

        try:
            if remote_sha == zero_sha:
                # New branch: commits not on any remote
                result = subprocess.run(
                    ["git", "log", "--format=%H", local_sha, "--not", "--remotes"],
                    capture_output=True, text=True, timeout=30,
                )
            else:
                # After a history rewrite (e.g. rlsbl release scrub), the old
                # remote head no longer resolves locally, which makes the
                # range empty. Previously that produced a silent, accidental
                # pass; make the skip explicit and loud instead. Pass/fail
                # semantics are unchanged.
                probe = subprocess.run(
                    ["git", "rev-parse", "--verify", "--quiet",
                     f"{remote_sha}^{{commit}}"],
                    capture_output=True, text=True, timeout=30,
                )
                if probe.returncode != 0:
                    print(
                        f"history rewrite detected: old remote head "
                        f"{remote_sha[:12]} unresolvable — coverage check "
                        f"skipped for this ref",
                        file=sys.stderr,
                    )
                    continue
                result = subprocess.run(
                    ["git", "log", "--format=%H", f"{remote_sha}..{local_sha}"],
                    capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if line:
                        commits.add(line)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    return commits


def _read_stdin_lines():
    """Read raw pre-push hook stdin lines.

    Returns a list of non-empty lines, or None if stdin is not readable
    or empty.
    """
    try:
        if sys.stdin.isatty():
            return None
        raw = sys.stdin.read().strip()
    except (AttributeError, OSError):
        return None

    if not raw:
        return None

    lines = [line for line in raw.splitlines() if line.strip()]
    return lines if lines else None


def _parse_stdin_refs(stdin_lines=None):
    """Parse pre-push hook stdin to extract (local_sha, remote_sha) pairs.

    Each line is: <local ref> <local sha> <remote ref> <remote sha>

    When *stdin_lines* is provided (a list of raw lines), parses those
    directly instead of reading stdin.  When omitted, reads stdin via
    ``_read_stdin_lines()``.

    Returns a list of (local_sha, remote_sha) tuples, or None if the
    input is empty or unreadable.
    """
    if stdin_lines is None:
        stdin_lines = _read_stdin_lines()
    if stdin_lines is None:
        return None

    refs = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) >= 4:
            local_sha = parts[1]
            remote_sha = parts[3]
            refs.append((local_sha, remote_sha))
    return refs if refs else None
