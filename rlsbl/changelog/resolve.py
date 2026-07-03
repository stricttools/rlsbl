"""Resolve abbreviated git commit hashes in changelog entries to full SHA-1 hashes and validate they exist in the repository."""

from __future__ import annotations

import subprocess

from ..utils import get_last_version_tag


def resolve_hash(hash_str: str, *, cwd: str | None = None) -> str | None:
    """Resolve a (possibly abbreviated) commit hash to a full 40-char SHA.

    ``cwd`` is the repository to resolve in; None means the process CWD
    (the historical behavior). Callers that may run from outside the
    target repo must pass it explicitly.

    Returns None if the hash doesn't resolve in that repo.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{hash_str}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        if result.returncode != 0:
            return None
        full_sha = result.stdout.strip()
        if len(full_sha) == 40:
            return full_sha
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def resolve_hashes(hashes: list[str], *, cwd: str | None = None) -> dict[str, str | None]:
    """Batch-resolve a list of commit hashes.

    ``cwd`` is the repository to resolve in; None means the process CWD.

    Returns a mapping from each input hash to its full 40-char SHA,
    or None if it doesn't resolve.
    """
    results: dict[str, str | None] = {}
    for h in hashes:
        if h not in results:
            results[h] = resolve_hash(h, cwd=cwd)
    return results


def _git_log_hashes(range_spec: str) -> list[str]:
    """Get commit hashes from git log for a given range spec.

    Returns a list of full 40-char SHAs, or empty list on error.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", range_spec],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_last_version_tag(tag_glob: str | None = None) -> str | None:
    """Backward-compatible wrapper around get_last_version_tag from utils.

    Accepts None for tag_glob (defaulting to "v*") to preserve the old call
    signature used by _unreleased_range and external callers like status.py.
    """
    return get_last_version_tag(tag_glob if tag_glob else "v*")


def _unreleased_range(tag_glob: str | None = None) -> str:
    """Return the git log range spec for unreleased commits.

    Uses <last_tag>..HEAD if a version tag exists, otherwise HEAD
    (all commits, for first release). Passes tag_glob through to
    _get_last_version_tag for monorepo-aware tag discovery.
    """
    tag = _get_last_version_tag(tag_glob)
    if tag:
        return f"{tag}..HEAD"
    return "HEAD"
