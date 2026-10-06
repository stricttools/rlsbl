"""Custom selfdoc directive: target-count.

Returns the project description sentence, with the number of release targets
beyond npm, PyPI and Go read from the committed support matrix
(``rlsbl/data/support-matrix.json``) rather than by importing rlsbl.

Output example:
``Release orchestration and project scaffolding for npm, PyPI, Go, and
[14 more release targets](https://rlsbl.smmh.dev/targets).``
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "rlsbl_docs_matrix", Path(__file__).with_name("_matrix.py")
)
_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_matrix)

# The three targets the sentence names outright; the link covers the rest.
NAMED = ("npm", "pypi", "go")


def resolve(attrs, config, body):
    """Return the project description with a derived target count link."""
    names = _matrix.target_names()
    missing = [n for n in NAMED if n not in names]
    if missing:
        raise ValueError(
            f"the support matrix has no row for {', '.join(missing)}; the "
            f"project description names them outright"
        )
    other = len(names) - len(NAMED)
    link = f"[{other} more release targets](https://rlsbl.smmh.dev/targets)"
    return f"Release orchestration and project scaffolding for npm, PyPI, Go, and {link}."
