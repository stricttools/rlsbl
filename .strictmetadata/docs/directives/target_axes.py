"""Custom selfdoc directive: table-target-axes.

Renders the support-axis inventory -- every question the release-target
model answers about a target -- from the committed support matrix
(``internal/targets/support-matrix.json``), which ``internal/targets/gen``
renders from the targets' facts.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "rlsbl_docs_matrix", Path(__file__).with_name("_matrix.py")
)
_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_matrix)


def resolve(attrs, config, body):
    """Return the support-axis inventory as a Markdown table."""
    return _matrix.render_table("axes", "No support axes declared.\n")
