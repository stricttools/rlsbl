"""The one place rlsbl builds a ``saferm delete`` argv.

Nine call sites used to spell the argv by hand, and the spelling has a
mandatory part that is easy to forget: saferm's ``--on-error`` flag is required
and has no default, so an invocation without it exits 1 *before deleting
anything*. Several sites omitted it and their deletions silently never
happened -- most visibly ``release retry``, which left ``retry.toml`` behind
and dirtied the working tree for the next release.

A source sweep can only ever catch the next omission after it is written. One
constructor makes the omission unwriteable: there is no parameter for the error
mode, because there is nothing to decide.
"""

from . import effects


def saferm_delete(
    path,
    *,
    description,
    recursive=False,
    skip_missing=False,
    install_hint=None,
):
    """Delete *path* through saferm, with the audit trail its description carries.

    Args:
        path: the file or directory to delete.
        description: WHY it is being deleted. Mandatory -- it is the audit
            trail, and saferm requires it.
        recursive: pass ``-r``; required for a directory.
        skip_missing: pass ``-f``, so an absent path is not an error.
        install_hint: when given, a missing saferm binary is re-raised as a
            RuntimeError ending with this sentence (e.g. "Install saferm before
            running cleanup."). Without it, FileNotFoundError propagates for
            the caller to handle.

    Raises:
        RuntimeError: saferm is not on PATH and *install_hint* was given.
        FileNotFoundError: saferm is not on PATH and it was not.
        subprocess.CalledProcessError: saferm exited non-zero.
    """
    argv = ["saferm", "delete"]
    if recursive:
        argv.append("-r")
    if skip_missing:
        argv.append("-f")
    argv += [
        "--description", description,
        # Mandatory and not a parameter: saferm has no default error mode, and
        # every rlsbl deletion wants the same one -- stop on the first failure
        # rather than press on through a partial delete.
        "--on-error", "abort",
        path,
    ]

    try:
        return effects.run(argv, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        if install_hint is None:
            raise
        raise RuntimeError(
            f"saferm is not installed or not on PATH. {install_hint}"
        ) from None
