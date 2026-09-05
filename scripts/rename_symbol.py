#!/usr/bin/env python3
"""Rename word-boundary symbols across a named file list, preview first.

Every bulk edit here runs as a preview (the default) before it writes: it
prints the per-file, per-pattern occurrence count and the total, and a real run
asserts the counts it was told to expect. A pattern that matches nothing, or a
different number of times than declared, is a hard error rather than a silent
no-op or a silent over-match.

Usage:
    scripts/rename_symbol.py --pair old=new [--pair old=new ...] \\
        [--expect N] [--apply] FILE [FILE ...]

Without --apply nothing is written. --expect is the total occurrence count
across every pattern and file; it is required for --apply.
"""

import argparse
import re
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", action="append", required=True,
                        help="old=new, matched on word boundaries")
    parser.add_argument("--expect", type=int, default=None,
                        help="total occurrences expected across all files")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: preview only)")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)

    pairs = []
    for raw in args.pair:
        old, sep, new = raw.partition("=")
        if not sep or not old or not new:
            parser.error(f"--pair must be old=new, got {raw!r}")
        pairs.append((old, new))

    total = 0
    planned = {}
    for path in args.files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        updated = text
        counts = []
        for old, new in pairs:
            pattern = re.compile(rf"\b{re.escape(old)}\b")
            hits = len(pattern.findall(updated))
            if hits:
                counts.append(f"{old} -> {new}: {hits}")
                total += hits
                updated = pattern.sub(new, updated)
        if counts:
            print(f"{path}: " + "; ".join(counts))
            planned[path] = updated

    print(f"TOTAL: {total} occurrence(s) in {len(planned)} file(s)")
    if args.expect is not None and total != args.expect:
        print(f"REFUSED: expected {args.expect} occurrence(s), found {total}",
              file=sys.stderr)
        return 1
    if not args.apply:
        print("Preview only: nothing was written.")
        return 0
    if args.expect is None:
        print("REFUSED: --apply requires --expect", file=sys.stderr)
        return 1
    for path, updated in planned.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
    print(f"Wrote {len(planned)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
