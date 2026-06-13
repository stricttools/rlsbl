"""Shared name-normalization utilities for release targets, handling package name canonicalization across different registry naming conventions."""

import re


def normalize_npm(name):
    """Normalize an npm package name for similarity comparison.

    Strips hyphens, underscores, dots, and lowercases.
    """
    return re.sub(r"[-_.]", "", name.lower())


def normalize_pypi(name):
    """Normalize a PyPI package name per PEP 503.

    Lowercases and replaces runs of [-_.] with a single hyphen.
    """
    return re.sub(r"[-_.]+", "-", name.lower())


def normalize_go(name):
    """Normalize a Go module path to a short name.

    Takes a module path like 'github.com/user/repo' and returns the last
    path segment lowercased (e.g., 'repo').
    """
    segment = name.rsplit("/", 1)[-1] if "/" in name else name
    return segment.lower()


def _get_git_author() -> str:
    """Return the git config user.name, or empty string on failure."""
    from ..utils import run
    try:
        return run("git", ["config", "user.name"])
    except Exception:
        return ""
