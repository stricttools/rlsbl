"""``rlsbl upstream adopt-tags``: move the tags a fork inherited from its
upstream out of ``refs/tags``.

The engine is :mod:`rlsbl.upstream`; this module runs it: observe under the
no-writes guard, print the plan, refuse on a conflict before writing anything,
and otherwise -- outside a dry run -- perform the moves.
"""

import sys

from .. import effects
from ..preview_apply import no_writes
from ..upstream import UpstreamError, apply, observe, refusal, render


def run_cmd(*, dry_run, start=None):
    """Adopt this repository's inherited tags. Exits non-zero on a refusal."""
    try:
        with no_writes():
            plan = observe(start)
    except UpstreamError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        effects.render_would_do_log()
    render(plan, sys.stdout)
    blocked = refusal(plan)
    if blocked:
        print(f"\nError: {blocked}", file=sys.stderr)
        sys.exit(1)

    work = plan.work
    if not work:
        print(
            f"\nNothing to do: no tag in refs/tags here or on origin is "
            f"inherited from {plan.upstream.url}."
        )
        return
    if dry_run:
        print(
            f"\nDry run: {len(work)} inherited tag(s) would move to "
            f"{plan.upstream.tags_of_prefix}. Nothing was written."
        )
        return
    try:
        apply(plan)
    except UpstreamError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    done = [
        (sum(t.create_kept for t in work), "kept ref(s) written here"),
        (sum(t.push_kept for t in work), "kept ref(s) pushed to origin"),
        (sum(t.delete_origin for t in work), "tag(s) deleted from origin's refs/tags"),
        (sum(t.delete_local for t in work), "tag(s) deleted from refs/tags here"),
    ]
    print(
        f"\nAdopted {len(work)} inherited tag(s) under "
        f"{plan.upstream.tags_of_prefix}: "
        + ", ".join(f"{n} {what}" for n, what in done if n) + "."
    )
