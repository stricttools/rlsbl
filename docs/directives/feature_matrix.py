"""Custom selfdoc directive: table-feature-matrix.

Renders the check-vs-target feature support matrix as a Markdown table.
Calls ``rlsbl.checks.generate_feature_matrix_markdown()`` which builds
the table from ``CHECK_TARGETS`` metadata.
"""

from rlsbl.checks import generate_feature_matrix_markdown


def resolve(attrs, config, body):
    """Return the feature matrix as a Markdown table."""
    return generate_feature_matrix_markdown()
