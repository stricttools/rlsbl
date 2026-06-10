"""Custom selfdoc directive: check-count.

Reads ``rlsbl/data/checks.toml`` and returns the total number of checks
and the number of distinct tags as a prose sentence.

Output example: ``rlsbl includes 49 checks across 6 tags.``
"""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def resolve(attrs, config, body):
    """Return a sentence with the check and tag counts from checks.toml."""
    checks_path = Path(__file__).resolve().parents[2] / "rlsbl" / "data" / "checks.toml"
    with open(checks_path, "rb") as f:
        data = tomllib.load(f)

    checks = data.get("checks", {})
    total = len(checks)

    tags = set()
    for check in checks.values():
        for tag in check.get("tags", []):
            tags.add(tag)

    return f"rlsbl includes {total} checks across {len(tags)} tags."
