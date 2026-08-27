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
    if f"import {WRAPPER}" in source or f"{WRAPPER}," in source.split("\n\n")[0]:
        return source
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            names = [a.name for a in node.names]
            if WRAPPER in names:
                return source
            names.append(WRAPPER)
            start = starts[node.lineno - 1]
            end = starts[node.end_lineno - 1] + len(lines[node.end_lineno - 1])
            return (
                source[:start]
                + f"from conftest import {', '.join(sorted(names))}\n"
                + source[end:]
            )
    last = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node
    if last is None:
        return f"from conftest import {WRAPPER}\n\n" + source
    end = starts[last.end_lineno - 1] + len(lines[last.end_lineno - 1])
    return source[:end] + f"\nfrom conftest import {WRAPPER}\n" + source[end:]


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
