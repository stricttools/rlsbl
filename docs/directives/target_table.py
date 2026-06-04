"""Custom selfdoc directive: table-targets.

Renders the release target table as a Markdown table.
Calls ``rlsbl.targets.introspect.generate_target_table_data()`` to get raw
table data, then renders it via ``selfdoc.tables.render_markdown_table()``.
"""

from rlsbl.targets.introspect import generate_target_table_data
from selfdoc.tables import render_markdown_table


def resolve(attrs, config, body):
    """Return the target table as a Markdown table."""
    headers, rows = generate_target_table_data()
    if not rows:
        return "No release targets registered.\n"
    return render_markdown_table(headers, rows)
