#!/usr/bin/env python3
"""Wrap hand-written workspace.toml test fixtures in ``conftest.workspace_toml``.

The workspace loader refuses a workspace with no root member and one with no
``[[releasables]]`` section. Most test fixtures write workspace.toml as raw
TOML text and are about neither, so they go through ``workspace_toml()``, which
supplies whatever the body does not already declare.

This rewrites ``<expr>.write_text(BODY)`` into
``<expr>.write_text(workspace_toml(BODY))`` for every ``<expr>`` naming a
workspace file, using the AST to find the exact argument span (so multi-line
bodies and trailing keyword arguments survive intact), and adds the conftest
import where it is missing.

Usage:
    scripts/sweep_workspace_fixtures.py --dry-run tests/test_*.py
    scripts/sweep_workspace_fixtures.py --apply   tests/test_*.py
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_conftest_import import ensure_conftest_import  # noqa: E402

WORKSPACE_MARKERS = ("workspace.toml", "WORKSPACE_FILE")
WRAPPER = "workspace_toml"


def _receiver_names_a_workspace_file(source: str, node: ast.Call) -> bool:
    """Does the ``.write_text`` receiver expression name a workspace file?"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "write_text":
        return False
    segment = ast.get_source_segment(source, func.value) or ""
    return any(marker in segment for marker in WORKSPACE_MARKERS)


def _plan(source: str) -> list[tuple[int, int, str]]:
    """Return (start_offset, end_offset, replacement) edits, last-first."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        return starts[lineno - 1] + col

    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _receiver_names_a_workspace_file(source, node):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        arg_source = ast.get_source_segment(source, arg) or ""
        if arg_source.lstrip().startswith(WRAPPER + "("):
            continue
        start = offset(arg.lineno, arg.col_offset)
        end = offset(arg.end_lineno, arg.end_col_offset)
        edits.append((start, end, f"{WRAPPER}({source[start:end]})"))
    edits.sort(reverse=True)
    return edits


def _ensure_import(source: str) -> str:
    """Add ``workspace_toml`` to the module's conftest import."""
    return ensure_conftest_import(source, WRAPPER)


def sweep(path: Path, apply: bool) -> bool:
    """Rewrite *path*; return True when it changed."""
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
        diff = difflib.unified_diff(
            source.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path), tofile=str(path) + " (planned)",
        )
        sys.stdout.writelines(diff)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the planned diff")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    changed = [p for p in args.paths if sweep(p, apply=args.apply)]
    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb} {len(changed)} file(s): {', '.join(p.name for p in changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
