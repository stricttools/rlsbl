#!/usr/bin/env python3
"""Rewrite the ``description`` field of archived release files.

An archived release file (``.rlsbl/releases/v{X.Y.Z}.toml``) is the source every
later changelog regeneration reads a version's description from, and it is
chmodded 0444 the instant it exists. Correcting a shipped description therefore
means unlocking the archive, rewriting one field, relocking it, and regenerating
-- which is exactly what this does, for one version or for a hundred.

The descriptions come from a JSON object mapping version to description, read
from a file or from stdin, so the texts are authored somewhere they can be
reviewed rather than typed into a command line.

Usage:
    scripts/set_archived_descriptions.py --map-file descriptions.json --dry-run
    cat descriptions.json | scripts/set_archived_descriptions.py --stdin

Every run prints a plan (old -> new per version). ``--dry-run`` stops there.
A named version with no archive is a hard error, and the number of files written
is compared against the number of entries whose description actually differs --
a silent no-op or a silent over-match is refused, not reported as success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import tomlkit

_SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SCRIPT_ROOT)

from rlsbl import effects  # noqa: E402
from rlsbl.release_file import writable_release_file  # noqa: E402
from rlsbl.utils import commit_files  # noqa: E402


def archive_path(repo: str, releases_dir: str, version: str) -> str:
    return os.path.join(repo, releases_dir, f"v{version}.toml")


def read_description(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return tomlkit.loads(f.read()).get("description", "")


def set_description(path: str, description: str) -> None:
    """Replace one field, preserving every other byte tomlkit can round-trip."""
    with writable_release_file(path):
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.loads(f.read())
        doc["description"] = description
        effects.atomic_write_text(path, tomlkit.dumps(doc))


def plan(repo: str, releases_dir: str, mapping: dict) -> tuple[list, list]:
    """Split the mapping into (changes, unchanged). Missing archives raise."""
    changes, unchanged = [], []
    missing = []
    for version in sorted(mapping):
        path = archive_path(repo, releases_dir, version)
        if not os.path.isfile(path):
            missing.append(version)
            continue
        current = read_description(path)
        entry = (version, path, current, mapping[version])
        (unchanged if current == mapping[version] else changes).append(entry)
    if missing:
        raise SystemExit(
            f"error: no archive for version(s): {', '.join(missing)} "
            f"(looked in {os.path.join(repo, releases_dir)})"
        )
    return changes, unchanged


def render(changes: list, unchanged: list, out=None) -> None:
    # Resolved at call time, never bound as a default: a default argument would
    # capture whatever sys.stdout was at import, which is the wrong stream for
    # any caller that redirects it.
    out = sys.stdout if out is None else out
    print(f"{len(changes)} description(s) to rewrite, {len(unchanged)} already current.", file=out)
    for version, path, current, new in changes:
        print(f"\n  v{version}  ({os.path.basename(path)})", file=out)
        print(f"    old: {current[:160]}", file=out)
        print(f"    new: {new[:160]}", file=out)
    for version, _path, _current, _new in unchanged:
        print(f"  v{version}: unchanged", file=out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite the description field of archived release files.",
    )
    parser.add_argument("--repo", default=".", help="repository to operate on (default: cwd)")
    parser.add_argument(
        "--releases-dir",
        default=os.path.join(".rlsbl", "releases"),
        help="releases directory, relative to the repo (default: .rlsbl/releases)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--map-file", help="JSON file mapping version -> description")
    source.add_argument("--stdin", action="store_true", help="read the JSON map from stdin")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    parser.add_argument("--no-commit", action="store_true", help="leave the rewrites uncommitted")
    parser.add_argument(
        "--message",
        default="Author descriptions for archived releases",
        help="commit message for the rewrite",
    )
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    raw = sys.stdin.read() if args.stdin else open(args.map_file, encoding="utf-8").read()
    mapping = json.loads(raw)
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in mapping.items()
    ):
        raise SystemExit("error: the map must be a JSON object of version -> non-empty description")

    changes, unchanged = plan(repo, args.releases_dir, mapping)
    render(changes, unchanged)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not changes:
        print("\nNothing to do.")
        return 0

    for _version, path, _current, new in changes:
        set_description(path, new)

    written = [os.path.relpath(p, repo) for _v, p, _c, _n in changes]
    applied = sum(
        1 for _v, p, _c, n in changes if read_description(p) == n
    )
    if applied != len(changes):
        raise SystemExit(
            f"error: wrote {len(changes)} file(s) but only {applied} carry the "
            f"intended description; inspect the working tree before committing."
        )
    print(f"\nRewrote {len(written)} description(s).")

    if not args.no_commit:
        commit_files(args.message, written, autogenerated=False, cwd=repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
