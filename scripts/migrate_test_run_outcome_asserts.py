#!/usr/bin/env python3
"""One-off sweep: retarget ``run_project_tests`` assertions at TestRunOutcome.

``run_project_tests`` used to return a bare bool, so an unsupported target and
a passing suite were the same value. It now returns a ``TestRunOutcome``, and
the tests that pinned the bool must pin the outcome instead.

The rewrite is scoped by *provenance*, not by pattern: a line is rewritten only
when the most recent ``result = ...`` assignment in the same file came from
``run_project_tests``. Other bool-returning helpers in the same files
(``sync_workspace``) keep their assertions untouched.

Dry run by default; ``--fix`` writes. Both modes print every line they would
change and exit non-zero if the observed count does not match ``--expect``.
"""

import argparse
import re
import sys

ASSIGN_RE = re.compile(r"^\s*result = (\w+)\(")
ASSIGN_CONT_RE = re.compile(r"^\s*result = (\w+)\($")
TRUE_RE = re.compile(r"^(\s*)assert result is True\s*$")
FALSE_RE = re.compile(r"^(\s*)assert result is False\s*$")


def transform(path):
    """Return (new_lines, changes) for one file."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out = []
    changes = []
    source = None
    for i, line in enumerate(lines, start=1):
        m = ASSIGN_RE.match(line)
        if m:
            source = m.group(1)
        if source == "run_project_tests":
            t = TRUE_RE.match(line)
            if t:
                new = f"{t.group(1)}assert result.passed\n"
                changes.append((i, line.rstrip(), new.rstrip()))
                out.append(new)
                continue
            f_ = FALSE_RE.match(line)
            if f_:
                new = f"{f_.group(1)}assert not result.passed\n"
                changes.append((i, line.rstrip(), new.rstrip()))
                out.append(new)
                continue
        out.append(line)
    return out, changes


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--expect", type=int, required=True)
    args = ap.parse_args()

    total = 0
    for path in args.files:
        new_lines, changes = transform(path)
        for lineno, before, after in changes:
            print(f"{path}:{lineno}\n  - {before}\n  + {after}")
        total += len(changes)
        if args.fix and changes:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

    print(f"\n{'applied' if args.fix else 'would change'}: {total} line(s)")
    if total != args.expect:
        print(f"EXPECTED {args.expect} -- refusing", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
