"""Shared utilities for release commands: project root detection, git auth verification, working tree guards, and release file loading."""


def build_release_flags(dry_run, yes, quiet, allow_dirty, watch=False,
                        push_timeout=None, ci_timeout=None,
                        check_timeout=None, hook_timeout=None):
    """Build the standard release flags dict from CLI arguments.

    The four timeout entries are the CLI overrides (``--push-timeout``,
    ``--ci-timeout``, ``--check-timeout``, ``--hook-timeout``). Each is None
    when its flag was not passed, in which case the matching config key and
    then the shipped default win.
    """
    return {
        "dry-run": dry_run,
        "yes": yes,
        "quiet": quiet,
        "allow-dirty": allow_dirty,
        "watch": bool(watch),
        "push-timeout": push_timeout,
        "ci-timeout": ci_timeout,
        "check-timeout": check_timeout,
        "hook-timeout": hook_timeout,
    }


# CLI flag -> config key, for the timeouts a release invocation can override.
_TIMEOUT_FLAG_KEYS = (
    ("push-timeout", "push_timeout"),
    ("ci-timeout", "ci_timeout"),
    ("check-timeout", "check_timeout"),
    ("hook-timeout", "hook_timeout"),
)


def apply_timeout_overrides(config, flags):
    """Write the release invocation's timeout flags into the in-memory config.

    ``--push-timeout`` and ``--ci-timeout`` reach their consumers directly (the
    executor holds both the flags dict and the config), but ``--check-timeout``
    and ``--hook-timeout`` are consumed by the check framework and the hook
    runner, which receive only a :class:`ProjectContext`. Their overrides
    therefore land on the config dict -- exactly the place every consumer
    already reads (``get_check_timeout`` / ``get_hook_timeout``), so one
    mechanism covers built-in checks, external checks, and hooks alike.

    A flag that was not passed is None and leaves the config untouched, so the
    documented precedence (flag > config key > shipped default) holds.
    """
    if config is None:
        return config
    for flag, key in _TIMEOUT_FLAG_KEYS:
        value = flags.get(flag)
        if value is not None:
            config[key] = value
    return config
