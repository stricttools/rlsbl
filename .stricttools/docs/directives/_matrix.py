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
selfdoc loads directive files individually rather than as a package. It also
carries the Markdown table renderer, so every directive that emits a table --
including the ones deriving from ``rlsbl/data/checks.toml`` rather than from the
matrix -- renders through one function. The renderer is vendored here rather
than imported: a directive script runs under whatever ``python3`` the site
generator invokes, with no packages installed for it, so a directive that
imported a library would break the whole docs build the moment that library was
absent.
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


def _escape_pipes(text):
    """Escape ``|`` so it cannot end a cell, leaving backtick spans alone.

    A pipe inside a code span is literal to a Markdown reader and must stay
    unescaped, or the rendered page shows a backslash. An opening backtick with
    no closing one is not a span at all, so its pipes are escaped like any
    other.
    """
    out = []
    span_start = None
    for ch in text:
        if ch == "`":
            span_start = len(out) if span_start is None else None
            out.append(ch)
        elif ch == "|" and span_start is None:
            out.append("\\|")
        else:
            out.append(ch)
    if span_start is not None:
        # The span never closed: re-escape everything after the stray backtick.
        out[span_start:] = [
            "\\|" if ch == "|" else ch for ch in out[span_start:]
        ]
    return "".join(out)


def _cell(value):
    """Return one cell's Markdown text, refusing a value that breaks the row."""
    text = str(value)
    if "\n" in text:
        raise ValueError("cell values must not contain newline characters")
    return _escape_pipes(text)


def render_markdown_table(headers, rows):
    """Render a GFM pipe table: header row, separator row, one row per record.

    A row with fewer cells than there are headers is padded with empty cells; a
    row with more is an error, since the extra cells would be dropped silently.
    """
    if not headers:
        raise ValueError("headers must not be empty")
    width = len(headers)

    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for index, row in enumerate(rows):
        cells = [_cell(c) for c in row]
        if len(cells) > width:
            raise ValueError(
                f"row {index} has {len(cells)} cells, but only {width} headers"
            )
        cells.extend([""] * (width - len(cells)))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_rows(headers, rows):
    """Render arbitrary headers and rows as Markdown, for non-matrix data."""
    return render_markdown_table(headers, rows)


def render_table(name, empty_message):
    """Render one of the matrix's committed tables as Markdown.

    *name* is a key of the artifact's ``tables`` section; *empty_message* is
    what to emit when that table has no rows.
    """
    table = load()["tables"][name]
    if not table["rows"]:
        return empty_message
    return render_markdown_table(table["headers"], table["rows"])


def target_names():
    """Every registered release target name, in the matrix's own order."""
    return sorted(load()["targets"])
