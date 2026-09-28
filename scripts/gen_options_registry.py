#!/usr/bin/env python3
"""Regenerate rlsbl's options registry (``rlsbl/data/options.toml``).

The registry is rendered from ``rlsbl/data/checks.toml`` by
:func:`rlsbl.options_registry.render`, then validated with strictspec before
it is written: a rendering strictspec refuses is never written.

Usage:
    uv run python scripts/gen_options_registry.py            # write
    uv run python scripts/gen_options_registry.py --check    # compare only
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import strictspec  # noqa: E402

from rlsbl.options_registry import REGISTRY_TOML, render  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare the committed file with a fresh rendering; write nothing")
    args = parser.parse_args()

    text = render()
    registry, diagnostics = strictspec.read_options_registry(text.encode())
    if registry is not None:
        _checked, diagnostics = strictspec.validate_options_registry(registry)
    if diagnostics:
        for d in diagnostics:
            print(f"{d.code} at {d.path}: {d.message}", file=sys.stderr)
        print("strictspec refuses the rendered registry; nothing was written.", file=sys.stderr)
        return 1

    relative = REGISTRY_TOML.relative_to(REPO_ROOT)
    current = REGISTRY_TOML.read_text(encoding="utf-8") if REGISTRY_TOML.exists() else None
    if args.check:
        if current == text:
            print(f"{relative} is fresh.")
            return 0
        print(f"{relative} is stale: run scripts/gen_options_registry.py to regenerate it.", file=sys.stderr)
        return 1
    if current == text:
        print(f"{relative} is already fresh.")
        return 0
    REGISTRY_TOML.write_text(text, encoding="utf-8")
    print(f"Wrote {relative}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
