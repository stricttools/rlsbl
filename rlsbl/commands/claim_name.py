"""Claim a package name on npm or PyPI by publishing a minimal placeholder package with version 0.0.0 and an empty description."""

import os
import subprocess
import sys

from .. import effects
from ..targets import TARGETS, claimable_targets


def run_cmd(target, args, flags):
    """Claim a package name on a registry by publishing a minimal placeholder.

    Checks availability first via check-name, then publishes a version 0.0.0
    placeholder package to reserve the name. Supports npm (via npm publish),
    and PyPI (via uv build + uv publish). Requires NPM_TOKEN for npm, or
    PYPI_TOKEN / UV_PUBLISH_TOKEN for PyPI.
    When --force-publish is passed, proceeds even if the name appears taken or
    the availability check returns an ambiguous status. That is a decision
    about the availability CHECK, deliberately not the framework's
    --approve-consequential, which only skips the confirmation prompt: skipping
    a prompt must never also override a "this name is taken" refusal.
    """

    if len(args) != 1:
        print("Expected exactly one package name.", file=sys.stderr)
        sys.exit(1)

    name = args[0]

    claimable = claimable_targets()
    if target not in claimable:
        supported = " or ".join(f"'{t}'" for t in sorted(claimable))
        print(
            f"Unsupported target: {target!r}. Must be {supported}.",
            file=sys.stderr,
        )
        sys.exit(1)
    target_obj = TARGETS[target]

    from rlsbl.commands.check import _check_single_name

    result = _check_single_name(name, target)
    status = result["status"]

    if status == "available":
        pass
    elif status == "taken":
        detail = result.get("note") or result.get("reason", "unknown reason")
        print(f"Name '{name}' appears taken on {target}: {detail}.", file=sys.stderr)
        if flags["force-publish"]:
            print("--force-publish passed, attempting publish anyway...")
        else:
            sys.exit(1)
    elif status == "error":
        error = result.get("error", "unknown error")
        print(f"Error checking '{name}' on {target}: {error}", file=sys.stderr)
        sys.exit(2)
    else:
        print(f"Ambiguous status '{status}' for '{name}' on {target}.", file=sys.stderr)
        if flags["force-publish"]:
            print("--force-publish passed, attempting publish anyway...")
        else:
            sys.exit(1)

    # Which environment variable authenticates a claim is the target's own
    # knowledge, declared alongside the publish routine it feeds.
    token_vars = target_obj.claim_token_env_vars
    if token_vars and not any(var in os.environ for var in token_vars):
        if len(token_vars) == 1:
            print(
                f"{token_vars[0]} environment variable is not set.",
                file=sys.stderr,
            )
        else:
            joined = " nor ".join(token_vars)
            print(
                f"Neither {joined} environment variable is set.",
                file=sys.stderr,
            )
        sys.exit(1)

    # No hand-rolled preview and no hand-rolled prompt: `claim-name` declares
    # itself `consequential`, so strictcli asks for confirmation before
    # dispatch (--approve-consequential skips it), and under --dry-run every
    # write and every publish below is RECORDED instead of performed.  The
    # preview is therefore the real code path -- the incident this command is
    # named for (a "dry run" that published for real) cannot recur by a branch
    # being forgotten.

    tmpdir = effects.mkdtemp()
    try:
        # The publish routine is the target's own; the command no longer
        # branches on the name to pick one.
        url = target_obj.claim_placeholder(name, tmpdir)
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        sys.exit(1)
    finally:
        effects.rmtree(tmpdir)

    if not effects.previewing():
        # A preview published nothing, so it must not claim it did; the
        # would-do log the framework prints is the preview's own report.
        print(
            f"Successfully claimed '{name}' on "
            f"{target_obj.registry_display_name}: {url}"
        )
