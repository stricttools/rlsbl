#!/usr/bin/env python3
"""Collapse hand-rolled atomic writes onto effects.atomic_write_text.

The codebase grew ~45 copies of the tmp-file + replace convention in three
shapes.  This one-shot codemod rewrites the two mechanical ones (plain
``f.write`` and ``json.dump``) to the chokepoint helper; the ``tempfile.mkstemp``
shape is left to a hand pass because its resulting file mode differs.

Usage:
    python3 scripts/consolidate_atomic_writes.py [--dry-run] [PATH ...]
"""

import argparse
import os
import re
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RLSBL_DIR = os.path.join(REPO_ROOT, "rlsbl")

# An optional "# Atomic write: ..." comment block precedes many of the copies;
# it describes exactly what the helper now documents, so it is absorbed.
COMMENT = r'(?:^[ \t]*#[^\n]*\n)*?'

SHAPE_PLAIN = re.compile(
    r'(?P<lead>(?:^[ \t]*#[^\n]*(?:atomic|Atomic)[^\n]*\n)*)'
    r'(?P<ind>[ \t]*)(?P<tmp>\w+) = (?P<target>[\w.\[\]"\']+) \+ "\.tmp"\n'
    r'(?P=ind)with effects\.open_write\((?P=tmp), "w", encoding="utf-8"\) as f:\n'
    r'(?P=ind)    f\.write\((?P<expr>[^\n]+)\)\n'
    r'(?P=ind)effects\.replace\((?P=tmp), (?P=target)\)\n',
    re.M,
)

SHAPE_JSON = re.compile(
    r'(?P<lead>(?:^[ \t]*#[^\n]*(?:atomic|Atomic)[^\n]*\n)*)'
    r'(?P<ind>[ \t]*)(?P<tmp>\w+) = (?P<target>[\w.\[\]"\']+) \+ "\.tmp"\n'
    r'(?P=ind)with effects\.open_write\((?P=tmp), "w", encoding="utf-8"\) as f:\n'
    r'(?P=ind)    json\.dump\((?P<obj>[\w.\[\]"\']+), f(?P<kw>[^\n]*)\)\n'
    r'(?P=ind)    f\.write\("\\n"\)\n'
    r'(?P=ind)effects\.replace\((?P=tmp), (?P=target)\)\n',
    re.M,
)


def _plain(m):
    return (
        f'{m.group("ind")}effects.atomic_write_text('
        f'{m.group("target")}, {m.group("expr")})\n'
    )


def _json(m):
    kw = m.group("kw").lstrip(",").strip()
    dumps = f'json.dumps({m.group("obj")}, {kw})' if kw else f'json.dumps({m.group("obj")})'
    return (
        f'{m.group("ind")}effects.atomic_write_text('
        f'{m.group("target")}, {dumps} + "\\n")\n'
    )


def files(paths):
    if paths:
        for p in paths:
            yield os.path.abspath(p)
        return
    for dirpath, dirnames, filenames in os.walk(RLSBL_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("templates", "__pycache__")]
        for name in sorted(filenames):
            if name.endswith(".py") and name != "effects.py":
                yield os.path.join(dirpath, name)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    total = 0
    for path in files(args.paths):
        source = open(path, encoding="utf-8").read()
        new, n1 = SHAPE_JSON.subn(_json, source)
        new, n2 = SHAPE_PLAIN.subn(_plain, new)
        if not (n1 + n2):
            continue
        compile(new, path, "exec")  # syntax guard
        total += n1 + n2
        print(f"{os.path.relpath(path, REPO_ROOT)}: {n1} json + {n2} plain")
        if not args.dry_run:
            open(path, "w", encoding="utf-8").write(new)
    print(f"\n{total} hand-rolled atomic write(s) consolidated"
          f"{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
