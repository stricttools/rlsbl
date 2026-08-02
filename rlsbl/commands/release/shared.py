"""Shared utilities for release commands: project root detection, git auth verification, working tree guards, and release file loading."""


def build_release_flags(dry_run, yes, quiet, allow_dirty, watch=False,
                        push_timeout=None):
    """Build the standard release flags dict from CLI arguments.

    ``push_timeout`` is the ``--push-timeout`` override (None when the flag was
    not passed, in which case the config key and then the shipped default win).
    """
    return {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
        "watch": bool(watch),
        "push-timeout": push_timeout,
    }
