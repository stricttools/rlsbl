#!/usr/bin/env python3
"""Stamp ``format_version`` into a repo's existing JSONL changelog lines.

This is the ONE-TIME per-repo bootstrap for the changelog format_version gate.
Historical finalized ``x.y.z.jsonl`` files and the current ``unreleased.jsonl``
predate the gate and carry no ``format_version``. Once stamped, a repo flips
``"changelog_format_version_enforced": true`` in ``.rlsbl/config.json`` and the
``changelog-format-version-gate`` check enforces the gate on every line.

Contract (per the strictspec fleet bootstrap design):

- STAMP-ONLY, never reshape. Each line is edited by inserting
  ``"format_version":1`` immediately after the opening ``{``; every other byte
  of the line is preserved exactly. Field order, spacing, and content of the
  original line are untouched -- so CHANGELOG.md regeneration is byte-identical
  before and after stamping (generation ignores ``format_version``).
- REFUSE ambiguity. A file containing any already-stamped line is refused
  (hard error) rather than silently re-stamped -- running twice is an error, so
  a partial or repeated run can never corrupt a file.
- Read-only aware. Finalized files (chmod 444) are unlocked to 644, rewritten,
  and re-locked to 444. Writable files (e.g. ``unreleased.jsonl``) keep their
  permissions.

Usage:
    scripts/stamp_changelog_format_version.py <changes-dir> [<changes-dir> ...] [--dry-run]

``<changes-dir>`` is a ``.rlsbl/changes/`` directory (or a releasable's
``changes/`` directory). Pass ``--dry-run`` to report what would change without
writing.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys

# Add project root to path so we can import rlsbl modules.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from rlsbl.changelog.schema import CURRENT_FORMAT_VERSION  # noqa: E402


class StampError(Exception):
    """A file could not be stamped (invalid JSON, or already stamped)."""


def line_is_stamped(raw: str) -> bool:
    """Whether a JSONL line already carries a ``format_version`` key."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise StampError("line is not a JSON object")
    return "format_version" in data


def stamp_line(raw: str) -> str:
    """Return ``raw`` with ``"format_version":N`` inserted after the opening ``{``.

    Purely additive: the original line's bytes are preserved verbatim, only the
    format_version key is prepended inside the object. Raises :class:`StampError`
    when the line is not a JSON object or is already stamped.
    """
    stripped = raw.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise StampError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StampError("line is not a JSON object")
    if "format_version" in data:
        raise StampError("line is already stamped with format_version")
    if not stripped.startswith("{"):
        raise StampError("line does not start with '{'")

    insert = f'"format_version":{CURRENT_FORMAT_VERSION}'
    body = stripped[1:]
    if body.lstrip().startswith("}"):
        # Empty object: no trailing comma needed.
        return "{" + insert + body
    return "{" + insert + "," + body


def _is_read_only(path: str) -> bool:
    mode = os.stat(path).st_mode
    return not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def stamp_file(path: str, *, dry_run: bool) -> int:
    """Stamp every line of one JSONL file. Returns the number of stamped lines.

    Refuses the whole file (raises :class:`StampError`) if ANY line is already
    stamped -- a stamped file must never be re-stamped. Preserves read-only
    (444) files by unlocking, rewriting, and re-locking.
    """
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    lines = original.split("\n")
    # Preserve a trailing newline exactly: split leaves a trailing "" element.
    out_lines: list[str] = []
    stamped = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            out_lines.append(line)
            continue
        try:
            if line_is_stamped(line.strip()):
                raise StampError(
                    f"{path}: line {i + 1} is already stamped -- refusing to "
                    f"re-stamp this file"
                )
            out_lines.append(stamp_line(line))
            stamped += 1
        except StampError as exc:
            raise StampError(f"{path}: line {i + 1}: {exc}") from exc

    if stamped == 0:
        return 0

    if dry_run:
        return stamped

    new_content = "\n".join(out_lines)
    was_ro = _is_read_only(path)
    if was_ro:
        os.chmod(path, 0o644)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    finally:
        if was_ro:
            os.chmod(path, 0o444)
    return stamped


def list_jsonl_files(changes_dir: str) -> list[str]:
    """Return all ``*.jsonl`` files in a changes dir (unreleased + versioned)."""
    if not os.path.isdir(changes_dir):
        raise StampError(f"not a directory: {changes_dir}")
    files = []
    for name in sorted(os.listdir(changes_dir)):
        if name.endswith(".jsonl"):
            files.append(os.path.join(changes_dir, name))
    return files


def stamp_changes_dir(changes_dir: str, *, dry_run: bool) -> dict[str, int]:
    """Stamp every JSONL file in a changes dir. Returns ``{path: lines_stamped}``.

    Fails atomically-per-file: a file that is already stamped raises before any
    of its lines are written. Files are processed in sorted order.
    """
    report: dict[str, int] = {}
    for path in list_jsonl_files(changes_dir):
        report[path] = stamp_file(path, dry_run=dry_run)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stamp format_version into a repo's JSONL changelog lines."
    )
    parser.add_argument(
        "changes_dirs",
        nargs="+",
        help="One or more .rlsbl/changes/ directories to stamp.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args(argv)

    total_files = 0
    total_lines = 0
    try:
        for changes_dir in args.changes_dirs:
            report = stamp_changes_dir(changes_dir, dry_run=args.dry_run)
            for path, n in report.items():
                rel = os.path.relpath(path)
                if n:
                    total_files += 1
                    total_lines += n
                    verb = "would stamp" if args.dry_run else "stamped"
                    print(f"{verb} {n} line(s): {rel}")
                else:
                    print(f"no lines to stamp (empty): {rel}")
    except StampError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    prefix = "dry-run: would stamp" if args.dry_run else "stamped"
    print(f"{prefix} {total_lines} line(s) across {total_files} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
