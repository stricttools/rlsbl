"""Custom selfdoc directive: table-checks.

Renders the checks of one tag, one row per check with its severity and the
one-line description the registry gives it, straight from
``internal/checks/checks.toml`` -- the registry rlsbl embeds and registers its
checks from -- so the per-tag tables in ``.strictmetadata/docs/checks.md`` are
never typed by hand.

Attributes:

- ``tag``: the tag whose checks are listed. Required; a tag no check carries
  is an error, so a renamed or retired tag cannot leave an empty table behind.

Output shape::

    | Check | Severity | Also tagged | What it verifies |
    | --- | --- | --- | --- |
    | `lock` | warn | | <description> |
    ...
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

CHECKS_PATH = _matrix.repo_root() / "internal" / "checks" / "checks.toml"


def resolve(attrs, config, body):
    """Return the checks carrying the tag ``attrs["tag"]`` as a Markdown table."""
    tag = attrs.get("tag", "")
    if not tag:
        raise ValueError("table-checks needs a tag attribute naming the tag to list")
    with open(CHECKS_PATH, "rb") as f:
        checks = tomllib.load(f).get("checks", {})

    rows = []
    for name in sorted(checks):
        check = checks[name]
        tags = check.get("tags", [])
        if tag not in tags:
            continue
        others = ", ".join(f"`{t}`" for t in tags if t != tag)
        rows.append([f"`{name}`", check["severity"], others, check["description"]])
    if not rows:
        raise ValueError(f"no check in {CHECKS_PATH.name} carries the tag {tag!r}")
    return _matrix.render_rows(["Check", "Severity", "Also tagged", "What it verifies"], rows)
