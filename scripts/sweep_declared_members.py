#!/usr/bin/env python3
"""Wrap ``load_workspace(...)`` results in ``conftest.declared_members``.

Every workspace now carries a root member, so a test asserting on the members
it declared has to filter it out. This rewrites assignments of the form
``<name> = load_workspace(<args>)`` into
``<name> = declared_members(load_workspace(<args>))`` and adds the conftest
import, using the AST for exact spans.

Usage:
    scripts/sweep_declared_members.py --dry-run tests/test_foo.py
    scripts/sweep_declared_members.py --apply   tests/test_foo.py
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_conftest_import import ensure_conftest_import  # noqa: E402

WRAPPER = "declared_members"


def _offsets(source: str):
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    return lambda lineno, col: starts[lineno - 1] + col


def _plan(source: str) -> list[tuple[int, int, str]]:
    tree = ast.parse(source)
    offset = _offsets(source)
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "load_workspace":
            continue
        start = offset(call.lineno, call.col_offset)
        end = offset(call.end_lineno, call.end_col_offset)
        edits.append((start, end, f"{WRAPPER}({source[start:end]})"))
    edits.sort(reverse=True)
    return edits


def _ensure_import(source: str) -> str:
    """Add the wrapper to the module's conftest import."""
    return ensure_conftest_import(source, WRAPPER)


def sweep(path: Path, apply: bool) -> bool:
    source = path.read_text(encoding="utf-8")
    edits = _plan(source)
    if not edits:
        return False
    updated = source
    for start, end, replacement in edits:
        updated = updated[:start] + replacement + updated[end:]
    updated = _ensure_import(updated)
    try:
        ast.parse(updated)
    except SyntaxError as exc:
        print(f"{path}: rewrite would not parse ({exc}); skipped", file=sys.stderr)
        return False
    if apply:
        path.write_text(updated, encoding="utf-8")
    else:
        sys.stdout.writelines(difflib.unified_diff(
            source.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path), tofile=str(path) + " (planned)",
        ))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    changed = [p for p in args.paths if sweep(p, apply=args.apply)]
    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb} {len(changed)} file(s): {', '.join(p.name for p in changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
