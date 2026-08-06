"""Shared utilities for release commands: project root detection, git auth verification, working tree guards, and release file loading."""

import os


def load_release_env(config):
    """Load the config's ``env_file`` into ``os.environ``. THE entry helper.

    Every command that can reach a deploy step, a release hook or a local
    publish pipeline must call this before it does -- ``release run``,
    ``release resume``, ``deploy``, and the batch orchestrator for its own
    root-level work. This used to live inline in ``_run_cmd_inner`` only, so a
    ``release resume`` re-ran exactly the steps that need those credentials
    (deploy, post-release hooks) with none of them loaded: the failure the
    resume existed to fix came back wearing a different error message.

    Returns the resolved ``env_file`` value, or ``None`` when none is
    configured. A configured-but-absent file raises
    :class:`~rlsbl.errors.ConfigError` (see :func:`rlsbl.config.load_env_file`).
    """
    env_file = (config or {}).get("env_file")
    if not env_file:
        return None

    from ...config import load_env_file

    load_env_file(env_file)
    # Historical alias: the shared env file names the Cloudflare account
    # ``CF_ACCOUNT_ID``; wrangler and the Cloudflare SDKs read
    # ``CLOUDFLARE_ACCOUNT_ID``. Mirrored, never overwritten.
    if "CF_ACCOUNT_ID" in os.environ and "CLOUDFLARE_ACCOUNT_ID" not in os.environ:
        os.environ["CLOUDFLARE_ACCOUNT_ID"] = os.environ["CF_ACCOUNT_ID"]
    return env_file


def build_release_flags(dry_run, quiet, allow_dirty, watch=False,
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

    Each value is validated BEFORE it is written, so an invalid flag value is
    reported against the flag ("Invalid --check-timeout value: -5") instead of
    resurfacing downstream as "Invalid check_timeout in .rlsbl/config.json" --
    a file the operator never touched.
    """
    from ...utils import validate_timeout_override

    for flag, key in _TIMEOUT_FLAG_KEYS:
        value = flags.get(flag)
        if value is None:
            continue
        # Validated whether or not there is a config to write into: an invalid
        # flag value is invalid on its own terms.
        validate_timeout_override(key, value)
        if config is not None:
            config[key] = value
    return config
