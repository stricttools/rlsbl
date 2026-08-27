#!/usr/bin/env python3
"""Regenerate the committed support matrix (``rlsbl/data/support-matrix.json``).

The artifact is the machine-readable answer to "what does every release target
support", plus the check-vs-target scope map and the pipeline table. The docs
directives read it instead of importing rlsbl, and the ``target-matrix-fresh``
check fails the release when the committed file no longer matches a fresh
regeneration.

Usage:
    uv run python scripts/generate_support_matrix.py            # write
    uv run python scripts/generate_support_matrix.py --check    # compare only
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from rlsbl.targets.introspect import (  # noqa: E402
    MATRIX_RELPATH,
    matrix_path,
    render_matrix,
    write_matrix,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed artifact is stale, without writing it",
    )
    args = parser.parse_args()

    path = matrix_path(REPO_ROOT)

    if args.check:
        fresh = render_matrix()
        current = Path(path).read_text(encoding="utf-8") if Path(path).exists() else None
        if current == fresh:
            print(f"{MATRIX_RELPATH} is up to date")
            return 0
        print(f"{MATRIX_RELPATH} is STALE -- rerun without --check", file=sys.stderr)
        return 1

    changed = write_matrix(REPO_ROOT)
    print(f"{MATRIX_RELPATH}: {'rewritten' if changed else 'already up to date'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
