"""Centralized loader for pinned GitHub Actions versions, providing a single source of truth that every rlsbl-scaffolded workflow consumes so action upgrades happen in one place.

Reads the version table from ``rlsbl/data/action_versions.toml`` and exposes
helpers for resolving an action to its pinned ``name@version`` string. The
table is the single source of truth for every workflow rlsbl scaffolds or
generates programmatically; bumping a version means editing the TOML and
re-running scaffold.

No implicit defaults: requesting an action that is not in the table raises
:class:`UnknownActionError` so missing entries fail loudly.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache


_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "action_versions.toml")


class UnknownActionError(KeyError):
    """Raised when an action is requested that is not in the version table.

    Subclasses :class:`KeyError` so existing ``try/except KeyError`` blocks
    keep working while still allowing more specific handling.
    """


@lru_cache(maxsize=1)
def _load_table() -> dict[str, str]:
    """Load and cache the TOML version table.

    Cached for the lifetime of the process; the file is read at most once.
    """
    with open(_DATA_PATH, "rb") as f:
        data = tomllib.load(f)
    # The TOML is a flat mapping of "owner/name" -> "version".
    return {str(k): str(v) for k, v in data.items()}


def get_action_version(action_name: str) -> str:
    """Return the pinned version string for ``action_name``.

    Example: ``get_action_version("actions/checkout")`` -> ``"v6"``.

    Raises :class:`UnknownActionError` if the action is not in the table.
    """
    table = _load_table()
    if action_name not in table:
        raise UnknownActionError(
            f"Action {action_name!r} is not pinned in "
            f"rlsbl/data/action_versions.toml. Add an entry to the table "
            f"instead of hard-coding a version."
        )
    return table[action_name]


def format_action(action_name: str) -> str:
    """Return the full ``name@version`` reference for ``action_name``.

    Example: ``format_action("actions/checkout")`` -> ``"actions/checkout@v6"``.

    Raises :class:`UnknownActionError` if the action is not in the table.
    """
    return f"{action_name}@{get_action_version(action_name)}"


def get_all_versions() -> dict[str, str]:
    """Return a copy of the full action-name -> version mapping.

    Useful for template substitution passes and consistency checks. The
    returned dict is a shallow copy so callers can mutate it freely.
    """
    return dict(_load_table())
