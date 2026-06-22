"""Shared utilities for release commands."""


def build_release_flags(dry_run, yes, quiet, allow_dirty, watch=False):
    """Build the standard release flags dict from CLI arguments."""
    return {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
        "watch": bool(watch),
    }
