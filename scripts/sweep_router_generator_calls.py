#!/usr/bin/env python3
"""Point test call sites at the router harness instead of the raw generator.

``_generate_router`` gained a required ``filters`` argument (a
:class:`rlsbl.router_filters.RouterFilters` derived from the whole workspace).
Tests that only care about job inlining go through ``tests/routerharness.py``
instead, which composes the two.

This rewrites CALL sites only -- ``_generate_router(`` -> ``generate_router(``.
Import statements differ per file and are left for a human edit, so a file
whose calls are rewritten will fail to import until its import line is fixed;
that is deliberate and visible.

Usage:
    scripts/sweep_router_generator_calls.py --dry-run tests/test_ci_scale.py ...
    scripts/sweep_router_generator_calls.py --apply   tests/test_ci_scale.py ...

``--expect N`` asserts the total number of rewritten call sites; a mismatch
(including zero) exits non-zero without writing anything.
"""

import argparse
import re
import sys

CALL_RE = re.compile(r"(?<![\w.])_generate_router\(")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print what would change")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--expect", type=int, default=None,
                        help="assert this many call sites are rewritten")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    total = 0
    for path in args.paths:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new_text, count = CALL_RE.subn("generate_router(", text)
        if not count:
            print(f"{path}: no call sites", file=sys.stderr)
            continue
        total += count
        print(f"{path}: {count} call site(s)")
        for i, (before, after) in enumerate(
            zip(text.splitlines(), new_text.splitlines()), start=1
        ):
            if before != after:
                print(f"  {i}: {before.strip()}")
                print(f"   -> {after.strip()}")
        if args.apply:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)

    print(f"total: {total}")
    if args.expect is not None and total != args.expect:
        print(f"expected {args.expect} call sites, found {total}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
