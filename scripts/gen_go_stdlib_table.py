#!/usr/bin/env python3
"""Regenerate the committed Go standard-library table (``rlsbl/data/go-stdlib-packages.json``).

``rlsbl check-name --target go`` refuses a candidate package name that equals the
name of a Go standard-library package, because every file importing both must
alias one of them. It reads that list from the committed table, never from a
Go toolchain at run time: the table is an input rlsbl owns, so the same
candidate gets the same answer on every machine.

The table is the union of ``go list std`` over every platform ``go tool dist
list`` names (with cgo enabled, so ``runtime/cgo`` is included), because a
single platform's listing omits packages such as ``syscall/js`` or
``log/syslog``. Import paths with an ``internal`` or ``vendor`` element are
dropped: nothing outside the standard library can import them, so they never
force an alias on a caller. The stamp records the toolchain that produced the
table; ``tests/test_go_stdlib_table.py`` compares it against a fresh listing
when the local toolchain is on the same release line.

Usage:
    uv run python scripts/gen_go_stdlib_table.py            # write
    uv run python scripts/gen_go_stdlib_table.py --check    # compare only
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLE_RELPATH = os.path.join("rlsbl", "data", "go-stdlib-packages.json")
GENERATOR_RELPATH = os.path.join("scripts", "gen_go_stdlib_table.py")
SOURCE_DESCRIPTION = (
    "union of `go list std` over every GOOS/GOARCH in `go tool dist list`, "
    "CGO_ENABLED=1; import paths with an internal or vendor element excluded"
)


def _go(args, env=None):
    return subprocess.run(
        ["go", *args], capture_output=True, text=True, check=True, timeout=120,
        env=env,
    ).stdout


def toolchain_version():
    """The local toolchain's version, without the experiment suffix (``go1.26.6``)."""
    raw = _go(["env", "GOVERSION"]).strip()
    return re.split(r"[\s-]", raw, maxsplit=1)[0]


def is_importable_path(path):
    """False for import paths only the standard library itself can import."""
    return not any(part in ("internal", "vendor") for part in path.split("/"))


def list_std_packages():
    """Every importable standard-library import path, across all platforms."""
    platforms = _go(["tool", "dist", "list"]).split()
    paths = set()
    for platform in platforms:
        goos, goarch = platform.split("/", 1)
        env = dict(os.environ, GOOS=goos, GOARCH=goarch, CGO_ENABLED="1")
        paths.update(_go(["list", "std"], env=env).split())
    return sorted(p for p in paths if is_importable_path(p))


def render_table(packages, go_version):
    document = {
        "generator": GENERATOR_RELPATH,
        "source": SOURCE_DESCRIPTION,
        "go_version": go_version,
        "packages": list(packages),
    }
    return json.dumps(document, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed table differs from a fresh listing, without writing it",
    )
    args = parser.parse_args()

    fresh = render_table(list_std_packages(), toolchain_version())
    path = REPO_ROOT / TABLE_RELPATH
    current = path.read_text(encoding="utf-8") if path.exists() else None

    if args.check:
        if current == fresh:
            print(f"{TABLE_RELPATH} is up to date")
            return 0
        print(f"{TABLE_RELPATH} is STALE -- rerun without --check", file=sys.stderr)
        return 1

    if current != fresh:
        path.write_text(fresh, encoding="utf-8")
        print(f"{TABLE_RELPATH}: rewritten")
    else:
        print(f"{TABLE_RELPATH}: already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
