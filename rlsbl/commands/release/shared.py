"""Shared utilities for release commands."""


def build_release_flags(dry_run, yes, quiet, allow_dirty, watch=False, watch_async=False):
    """Build the standard release flags dict from CLI arguments.

    watch and watch_async come from a strictcli MutexGroup, so at most one
    is truthy; the unset member arrives as None and is coerced to False.
    """
    return {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
        "watch": bool(watch),
        "watch-async": bool(watch_async),
    }
