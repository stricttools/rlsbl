"""Custom selfdoc directive: target-count.

Returns a sentence fragment with the count of additional release targets
(beyond npm, PyPI, and Go) as a linked phrase.

Output example: ``[15 more release targets](https://rlsbl.smmh.dev/targets)``
"""

from rlsbl.targets import TARGETS


def resolve(attrs, config, body):
    """Return a linked phrase with the count of additional targets."""
    count = len(TARGETS)
    other = count - 3  # subtract npm, PyPI, Go
    return f"[{other} more release targets](https://rlsbl.smmh.dev/targets)"
