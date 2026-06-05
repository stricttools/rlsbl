"""Custom selfdoc directive: table-pipelines.

Renders the pipeline type table as a Markdown table.
Calls ``rlsbl.pipelines.introspect.generate_pipeline_table_data()`` to get raw
table data, then renders it via ``selfdoc.tables.render_markdown_table()``.
"""

from rlsbl.pipelines.introspect import generate_pipeline_table_data
from selfdoc.tables import render_markdown_table


def resolve(attrs, config, body):
    """Return the pipeline table as a Markdown table."""
    headers, rows = generate_pipeline_table_data()
    if not rows:
        return "No pipeline types registered.\n"
    return render_markdown_table(headers, rows)
