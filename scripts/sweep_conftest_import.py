#!/usr/bin/env python3
"""The conftest-import rewriter the fixture sweep scripts share.

Every sweep script wraps some expression in a ``conftest`` helper and then has
to make sure the module imports it.  Rebuilding that import is the one part of
a sweep that can silently damage a file it was not asked to change, so it lives
here once, with a test, rather than in three hand-copied versions:

    from conftest import git_head, run_git as _run_git

Rebuilding the names from ``node.names`` alone drops ``as _run_git`` and leaves
the module calling a name it no longer imports.  This module renders each alias
back, so the only difference between the old and the new import line is the
name that was added.
"""

from __future__ import annotations

import ast


def render_alias(alias: ast.alias) -> str:
    """Render one imported name, keeping its ``as`` clause."""
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def render_import(names) -> str:
    """Render a ``from conftest import ...`` line for *names*.

    *names* is an iterable of rendered names (see :func:`render_alias`), sorted
    here by the name being imported so the added one slots in deterministically
    and an ``as`` clause never changes an entry's position.
    """
    return f"from conftest import {', '.join(sorted(names, key=lambda n: n.split(' as ')[0]))}\n"


def ensure_conftest_import(source: str, wrapper: str) -> str:
    """Return *source* with ``wrapper`` imported from conftest.

    Unchanged when it is already imported.  Otherwise the module's existing
    conftest import is rebuilt with the new name added -- aliases intact -- or
    a fresh import is inserted after the last top-level import.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            rendered = [render_alias(a) for a in node.names]
            if any(a.name == wrapper for a in node.names):
                return source
            rendered.append(wrapper)
            start = starts[node.lineno - 1]
            end = starts[node.end_lineno - 1] + len(lines[node.end_lineno - 1])
            return source[:start] + render_import(rendered) + source[end:]

    last = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node
    if last is None:
        return f"from conftest import {wrapper}\n\n" + source
    end = starts[last.end_lineno - 1] + len(lines[last.end_lineno - 1])
    return source[:end] + f"\nfrom conftest import {wrapper}\n" + source[end:]
