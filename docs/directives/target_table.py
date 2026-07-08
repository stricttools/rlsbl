"""Custom selfdoc directive: table-targets.

Renders the release target table as a Markdown table.
Calls ``rlsbl.targets.introspect.generate_target_table_data()`` to get raw
table data, then renders it via ``selfdoc.tables.render_markdown_table()``.
"""

from rlsbl.targets.introspect import generate_target_table_data

try:
    from selfdoc_core.tables import render_markdown_table
except ImportError:
    render_markdown_table = None


def resolve(attrs, config, body):
    """Return the target table as a Markdown table."""
    if render_markdown_table is None:
        raise ImportError(
            "selfdoc-core is required for this directive. "
            "Install with: pip install rlsbl[docs]"
        )
    headers, rows = generate_target_table_data()
    if not rows:
        return "No release targets registered.\n"
    return render_markdown_table(headers, rows)
