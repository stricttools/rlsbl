"""Custom selfdoc directive: target-count.

Returns a project description sentence that includes the dynamic count
of release targets beyond npm, PyPI, and Go as a linked phrase.

Output example:
``Release orchestration and project scaffolding for npm, PyPI, Go, and
[15 more release targets](https://rlsbl.smmh.dev/targets).``
"""

from rlsbl.targets import TARGETS


def resolve(attrs, config, body):
    """Return the project description with a dynamic target count link."""
    count = len(TARGETS)
    other = count - 3  # subtract npm, PyPI, Go
    link = f"[{other} more release targets](https://rlsbl.smmh.dev/targets)"
    return f"Release orchestration and project scaffolding for npm, PyPI, Go, and {link}."
