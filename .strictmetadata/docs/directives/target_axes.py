"""Custom selfdoc directive: table-target-axes.

Renders the support-axis inventory -- every question the release-target
protocol answers about a target -- from the committed support matrix
(``rlsbl/data/support-matrix.json``). The inventory is declared once, in
``rlsbl.targets.introspect.TARGET_AXES``, and an axis missing from it is an
import-time error, so this table cannot fall behind the protocol.
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
