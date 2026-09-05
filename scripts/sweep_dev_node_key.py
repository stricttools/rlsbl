#!/usr/bin/env python3
"""Rewrite the deleted ``dev_node`` member key into the two-key form.

A dev node declares two things: ``dev_only = true`` (what it IS) and
``releasable = false`` (where it SITS). The single ``dev_node`` key that used
to stand for both is deleted and refused at load, so every fixture and helper
that still writes it has to state both.

The sweep handles the two mechanical spellings:

- a Python dict literal entry ``"dev_node": True`` -> ``"dev_only": True,
  "releasable": False``. The rewrite is per-entry and does not read the
  enclosing literal, so a literal that ALREADY carries ``dev_only`` or
  ``releasable`` must be fixed by hand first (the dry run's per-file counts are
  how you find them);
- a TOML line ``dev_node = true`` inside a test's workspace text ->
  ``dev_only = true`` plus ``releasable = false``.

Everything else -- helper parameters named ``dev_node``, tests ABOUT the legacy
key -- is left for a human, and reported.

Usage:
    scripts/sweep_dev_node_key.py --dry-run <path>...
    scripts/sweep_dev_node_key.py --apply <path>...
"""

import re
import sys


DICT_ENTRY = re.compile(r'"dev_node":\s*True')
TOML_LINE = re.compile(r"^(\s*)dev_node = true$", re.MULTILINE)
TOML_IN_STRING = re.compile(r"dev_node = true\\n")


def rewrite(text):
    """Return (new_text, counts) for one file's source."""
    counts = {"dict": 0, "toml_line": 0, "toml_in_string": 0}

    def dict_sub(match):
        counts["dict"] += 1
        return '"dev_only": True, "releasable": False'

    def toml_line_sub(match):
        counts["toml_line"] += 1
        indent = match.group(1)
        return f"{indent}dev_only = true\n{indent}releasable = false"

    def toml_string_sub(match):
        counts["toml_in_string"] += 1
        return "dev_only = true\\nreleasable = false\\n"

    text = DICT_ENTRY.sub(dict_sub, text)
    text = TOML_LINE.sub(toml_line_sub, text)
    text = TOML_IN_STRING.sub(toml_string_sub, text)
    return text, counts


def main(argv):
    if len(argv) < 2 or argv[0] not in ("--dry-run", "--apply"):
        print(__doc__, file=sys.stderr)
        return 2
    apply_changes = argv[0] == "--apply"
    total = 0
    for path in argv[1:]:
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        new, counts = rewrite(original)
        changed = sum(counts.values())
        if changed:
            total += changed
            print(f"{path}: {counts}")
            if apply_changes:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(new)
        leftover = new.count("dev_node")
        if leftover:
            print(f"  {path}: {leftover} remaining 'dev_node' mention(s) for review")
    print(f"total rewrites: {total} ({'applied' if apply_changes else 'dry run'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
