#!/usr/bin/env python3
"""Wrap ``save_workspace``'s member list in ``conftest.with_root_member``.

Every workspace must declare a root member, so a test that builds a member list
by hand and hands it to ``save_workspace`` needs one added. This rewrites the
second positional argument of every ``save_workspace(...)`` call, using the AST
for the exact span (multi-line list literals included), and adds the conftest
import where it is missing.

Usage:
    scripts/sweep_save_workspace_root.py --dry-run tests/test_foo.py
    scripts/sweep_save_workspace_root.py --apply   tests/test_foo.py
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

WRAPPER = "with_root_member"


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
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "save_workspace" or len(node.args) < 2:
            continue
        arg = node.args[1]
        arg_source = ast.get_source_segment(source, arg) or ""
        if arg_source.lstrip().startswith(WRAPPER + "("):
            continue
        start = offset(arg.lineno, arg.col_offset)
        end = offset(arg.end_lineno, arg.end_col_offset)
        edits.append((start, end, f"{WRAPPER}({source[start:end]})"))
    edits.sort(reverse=True)
    return edits


def _ensure_import(source: str) -> str:
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
