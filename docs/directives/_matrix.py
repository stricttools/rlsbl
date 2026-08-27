"""Shared reader for the committed support matrix.

Every table and count the docs derive from rlsbl's registries comes from
``rlsbl/data/support-matrix.json``, which ``rlsbl/targets/introspect.py``
generates and the ``target-matrix-fresh`` check keeps in step with the code.

The directives used to import rlsbl and call its introspection functions at
documentation-build time. That put the whole package on the docs environment's
dependency list, and a release once failed when that environment lost its rlsbl
overlay. Reading a committed file has no such failure mode: the artifact is
part of the repository, and a stale one is a check failure rather than a
silently wrong page.

This module is loaded by each directive through ``importlib`` by path, because
selfdoc loads directive files individually rather than as a package.
"""

import json
from pathlib import Path

MATRIX_PATH = (
    Path(__file__).resolve().parents[2] / "rlsbl" / "data" / "support-matrix.json"
)


def load():
    """Return the whole support matrix document."""
    with open(MATRIX_PATH, encoding="utf-8") as f:
        return json.load(f)


def _renderer():
    """Return selfdoc-core's markdown table renderer, or say what is missing."""
    try:
        from selfdoc_core.tables import render_markdown_table
    except ImportError as exc:
        raise ImportError(
            "selfdoc-core is required for this directive. "
            "Install with: pip install rlsbl[docs]"
        ) from exc
    return render_markdown_table


def render_table(name, empty_message):
    """Render one of the matrix's committed tables as Markdown.

    *name* is a key of the artifact's ``tables`` section; *empty_message* is
    what to emit when that table has no rows.
    """
    render_markdown_table = _renderer()
    table = load()["tables"][name]
    if not table["rows"]:
        return empty_message
    return render_markdown_table(table["headers"], table["rows"])


def target_names():
    """Every registered release target name, in the matrix's own order."""
    return sorted(load()["targets"])
