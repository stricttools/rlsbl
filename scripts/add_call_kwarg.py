#!/usr/bin/env python3
"""Append a keyword argument to every call of a named function in given files.

Usage: add_call_kwarg.py [--dry-run] --func NAME --kwarg 'k=v' --expect N FILE...

Matches ``NAME(`` not preceded by an identifier character or ``def ``, finds
the matching close parenthesis (string literals are skipped), and inserts
``, k=v`` before it (or ``k=v`` when the argument list is empty; a trailing
comma is reused). The total number of calls changed must equal --expect,
otherwise nothing is written.
"""
import argparse
import difflib
import re
import sys


def find_close(s, i):
    depth = 0
    quote = None
    j = i
    while j < len(s):
        c = s[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if s.startswith(quote, j):
                j += len(quote)
                quote = None
                continue
        elif s.startswith('"""', j) or s.startswith("'''", j):
            quote = s[j:j + 3]
            j += 3
            continue
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ValueError("unbalanced parentheses")


def transform(s, func, kwarg):
    pat = re.compile(r"(?<![\w.])(?<!def )" + re.escape(func) + r"\(")
    out = []
    pos = 0
    n = 0
    for m in pat.finditer(s):
        if m.start() < pos:
            continue
        open_at = m.end() - 1
        close = find_close(s, open_at)
        inner = s[open_at + 1:close]
        stripped = inner.rstrip()
        if not stripped:
            new_inner = kwarg
        elif stripped.endswith(","):
            new_inner = stripped + " " + kwarg + "," + inner[len(stripped):]
        else:
            new_inner = stripped + ", " + kwarg + inner[len(stripped):]
        out.append(s[pos:open_at + 1])
        out.append(new_inner)
        pos = close
        n += 1
    out.append(s[pos:])
    return "".join(out), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--func", required=True)
    ap.add_argument("--kwarg", required=True)
    ap.add_argument("--expect", type=int, required=True)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    results = {}
    total = 0
    for f in a.files:
        src = open(f).read()
        new, n = transform(src, a.func, a.kwarg)
        results[f] = (src, new)
        total += n
    if total != a.expect:
        print(f"expected {a.expect} calls, found {total}; nothing written", file=sys.stderr)
        sys.exit(1)
    for f, (src, new) in results.items():
        if a.dry_run:
            sys.stdout.writelines(difflib.unified_diff(
                src.splitlines(True), new.splitlines(True), f, f, n=0))
        elif new != src:
            open(f, "w").write(new)
    print(f"{total} call(s) {'would change' if a.dry_run else 'changed'}")


if __name__ == "__main__":
    main()
