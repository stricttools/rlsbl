"""Custom selfdoc directive: table-feature-matrix.

Renders the check-vs-target feature support matrix as a Markdown table.
Calls ``rlsbl.checks.generate_feature_matrix_data()`` to get raw matrix
data, then renders it via ``selfdoc.tables.render_markdown_table()``.
"""

from rlsbl.checks import generate_feature_matrix_data

try:
    from selfdoc_core.tables import render_markdown_table
except ImportError:
    render_markdown_table = None


def resolve(attrs, config, body):
    """Return the feature matrix as a Markdown table."""
    if render_markdown_table is None:
        raise ImportError(
            "selfdoc-core is required for this directive. "
            "Install with: pip install rlsbl[docs]"
        )
    headers, rows = generate_feature_matrix_data()
    if not rows:
        return "No target-specific checks registered.\n"
    return render_markdown_table(headers, rows)
