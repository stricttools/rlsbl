"""Custom selfdoc directive: table-pipelines.

Renders the pipeline type table as a Markdown table.
Calls ``rlsbl.pipelines.introspect.generate_pipeline_table_data()`` to get raw
table data, then renders it via ``selfdoc.tables.render_markdown_table()``.
"""

from rlsbl.pipelines.introspect import generate_pipeline_table_data

try:
    from selfdoc_core.tables import render_markdown_table
except ImportError:
    render_markdown_table = None


def resolve(attrs, config, body):
    """Return the pipeline table as a Markdown table."""
    if render_markdown_table is None:
        raise ImportError(
            "selfdoc-core is required for this directive. "
            "Install with: pip install rlsbl[docs]"
        )
    headers, rows = generate_pipeline_table_data()
    if not rows:
        return "No pipeline types registered.\n"
    return render_markdown_table(headers, rows)
