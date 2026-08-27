#!/usr/bin/env python3
"""Point Releasable tag-format reads at ``effective_tag_format``.

``Releasable.tag_format`` now carries the DECLARED value and may be absent
(``rlsbl.workspace_types.TAG_FORMAT_ABSENT``). Everything that needs a format
to work with reads ``effective_tag_format`` instead.

Only attribute reads are rewritten -- ``x.tag_format`` NOT followed by ``(``.
A target's ``tag_format(version)`` is a method call on a different object and
is left alone, as is ``rlsbl/workspace.py``, which is the one module that must
see the declared value (the loader and ``save_workspace``).

Usage:
    scripts/sweep_effective_tag_format.py --dry-run [--expect N] <paths...>
    scripts/sweep_effective_tag_format.py --apply   [--expect N] <paths...>
"""

import argparse
import re
import sys

READ_RE = re.compile(r"\.tag_format\b(?!\s*\()")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--expect", type=int, default=None)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    total = 0
    for path in args.paths:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new_text, count = READ_RE.subn(".effective_tag_format", text)
        if not count:
            continue
        total += count
        print(f"{path}: {count}")
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
        print(f"expected {args.expect}, found {total}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
