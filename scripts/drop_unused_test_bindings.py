#!/usr/bin/env python3
"""Drop assigned-but-unused local bindings that ruff's F841 reports in a tree.

The three shapes, chosen per site by what the line actually is:

* ``with ctx() as name:``       -> ``with ctx():``      (``--as``)
* ``name = call(...)``          -> ``call(...)``        (``--bare``)
* ``name = expr``               -> the line is removed  (``--delete``)

Each site is named as ``path:line:name``, matching what
``ruff check --select F841 --output-format concise`` prints, so a plan can be
built straight from ruff's own output.  The line's current text must still
contain the name, or the site is refused: a plan built against a tree that has
since moved must not rewrite the wrong line.

Always dry-run first (the default): it prints the before/after of every site and
writes nothing.  ``--apply`` performs the rewrite.
"""

import argparse
import re
import sys


def _rewrite(line, name, action):
    """Return the rewritten *line*, or None when the shape does not match."""
    if action == "delete":
        return ""
    if action == "as":
        pattern = re.compile(r"\s+as\s+" + re.escape(name) + r"\b")
        rewritten, count = pattern.subn("", line, count=1)
        return rewritten if count == 1 else None
    if action == "bare":
        pattern = re.compile(r"^(\s*)" + re.escape(name) + r"\s*=\s*(?=\S)")
        rewritten, count = pattern.subn(r"\1", line, count=1)
        return rewritten if count == 1 else None
    raise ValueError(f"unknown action {action!r}")


def _sites(spec_lines):
    for raw in spec_lines:
        spec = raw.split("#", 1)[0].strip()
        if not spec:
            continue
        path, line_no, name, action = spec.split(":")
        yield path, int(line_no), name, action


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plan",
        help="file of `path:line:name:action` sites, one per line ('-' for stdin)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="perform the rewrite (without it, print the plan and write nothing)",
    )
    args = parser.parse_args(argv)

    spec_lines = (
        sys.stdin.readlines() if args.plan == "-"
        else open(args.plan, encoding="utf-8").readlines()
    )

    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for path, line_no, name, action in _sites(spec_lines):
        by_file.setdefault(path, []).append((line_no, name, action))

    refused = []
    changed = 0
    for path, sites in sorted(by_file.items()):
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        for line_no, name, action in sorted(sites, reverse=True):
            original = lines[line_no - 1]
            if not re.search(r"\b" + re.escape(name) + r"\b", original):
                refused.append(f"{path}:{line_no}: {name!r} not on that line")
                continue
            rewritten = _rewrite(original, name, action)
            if rewritten is None:
                refused.append(f"{path}:{line_no}: {action} shape did not match")
                continue
            print(f"{path}:{line_no} [{action}]")
            print(f"  - {original.rstrip()}")
            print(f"  + {rewritten.rstrip() if rewritten else '(line removed)'}")
            if rewritten:
                lines[line_no - 1] = rewritten
            else:
                del lines[line_no - 1]
            changed += 1
        if args.apply:
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines)

    print(f"\n{changed} site(s) {'rewritten' if args.apply else 'planned'}")
    for message in refused:
        print(f"REFUSED {message}", file=sys.stderr)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
