#!/usr/bin/env python3
"""One-shot migration: rewrite literal ``uses: name@version`` lines in
templates as ``uses: {{action "name"}}`` placeholders so the central
action-version table is the only place a version lives.

- Walks every ``.tpl`` under ``rlsbl/templates/``.
- For each ``uses: <name>@<version>`` line where ``<name>`` is a third-party
  ``owner/repo`` ref and ``<version>`` is non-whitespace, replaces with
  ``uses: {{action "<name>"}}``.
- Preserves leading indentation, the optional list dash, and anything that
  follows the version (whitespace + inline ``# comment``).
- Skips local ``./`` refs and lines that already use a placeholder.
- Idempotent: re-running produces no changes.

Run from the project root::

    ./scripts/migrate-templates-to-action-placeholders.py
"""

from __future__ import annotations

import os
import re
import sys


TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rlsbl",
    "templates",
)

# Capture groups:
#   1: leading whitespace + optional list dash + "uses:" + spaces
#   2: action name (owner/repo, possibly with /subpath)
#   3: version (non-whitespace)
#   4: trailing whitespace + optional inline comment (e.g. "  # pinned")
_USES_RE = re.compile(
    r"^(\s*-?\s*uses:\s*)([A-Za-z0-9_][\w./-]*\/[\w./-]+)@(\S+)(\s*(?:#.*)?)$"
)


def _rewrite_line(line: str) -> tuple[str, bool]:
    """Return ``(new_line, changed)``. Local refs and placeholders pass through."""
    m = _USES_RE.match(line.rstrip("\n"))
    if not m:
        return line, False
    prefix, name, _version, trailing = m.groups()
    if name.startswith("."):
        return line, False
    new = f'{prefix}{{{{action "{name}"}}}}{trailing}'
    # Preserve trailing newline if the original had one.
    if line.endswith("\n"):
        new += "\n"
    return new, new != line


def _walk_templates() -> list[str]:
    paths: list[str] = []
    for root, _dirs, files in os.walk(TEMPLATES_ROOT):
        for name in files:
            if name.endswith(".tpl"):
                paths.append(os.path.join(root, name))
    paths.sort()
    return paths


def main() -> int:
    total_replacements = 0
    files_changed: list[tuple[str, int]] = []

    for path in _walk_templates():
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines: list[str] = []
        changed_in_file = 0
        for line in lines:
            new_line, changed = _rewrite_line(line)
            new_lines.append(new_line)
            if changed:
                changed_in_file += 1

        if changed_in_file:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            total_replacements += changed_in_file
            files_changed.append((path, changed_in_file))

    print(f"Migration summary:")
    print(f"  files changed: {len(files_changed)}")
    print(f"  total replacements: {total_replacements}")
    for path, n in files_changed:
        rel = os.path.relpath(path)
        print(f"    {rel}: {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
