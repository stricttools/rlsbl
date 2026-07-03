"""Resolve abbreviated git commit hashes in changelog entries to full SHA-1 hashes and validate they exist in the repository."""

from __future__ import annotations

import subprocess


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
