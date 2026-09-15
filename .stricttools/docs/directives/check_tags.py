"""Custom selfdoc directive: table-check-tags.

Renders one row per check tag, with the number of checks carrying it, straight
from ``rlsbl/data/checks.toml`` -- the same file the check runner registers
from, and the same file the ``check-count`` sentence reads.

The README used to hand-type this table. Every count in it had drifted below
what the registry held, and its per-row descriptions enumerated checks that had
since been renamed, merged or retired. Counts derived from the registry cannot
do that; what each tag's checks actually are stays in ``docs/checks.md``, whose
per-tag tables are verified against this same file by
``tests/test_docs_check_tables.py``.

Output shape::

    | Tag | Checks |
    | --- | --- |
    | `project` | <count> |
    ...
    | (untagged) | <count> |
"""

import importlib.util
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

_spec = importlib.util.spec_from_file_location(
    "rlsbl_docs_matrix", Path(__file__).with_name("_matrix.py")
)
_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_matrix)

CHECKS_PATH = (
    Path(__file__).resolve().parents[2] / "rlsbl" / "data" / "checks.toml"
)

# The row for checks that carry no tag at all: they run only under `--all` or
# `--name`, so they are part of the picture the table gives.
UNTAGGED_LABEL = "(untagged)"


def resolve(attrs, config, body):
    """Return the per-tag check-count table as Markdown."""
    with open(CHECKS_PATH, "rb") as f:
        checks = tomllib.load(f).get("checks", {})

    counts = {}
    untagged = 0
    for check in checks.values():
        tags = check.get("tags", [])
        if not tags:
            untagged += 1
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1

    rows = [
        [f"`{tag}`", str(count)]
        for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if untagged:
        rows.append([UNTAGGED_LABEL, str(untagged)])
    if not rows:
        return "No checks registered.\n"
    return _matrix.render_rows(["Tag", "Checks"], rows)
