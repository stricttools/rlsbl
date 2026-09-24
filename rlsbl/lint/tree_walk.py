"""Iterative traversal of tree-sitter syntax trees, and the error that names a source file which could not be analyzed.

A syntax tree is as deep as the source's deepest nesting, and generated code
nests arbitrarily deep: a protobuf descriptor written as one string
concatenation of a few thousand terms is a left-leaning binary expression a few
thousand levels deep.  A walker that recurses once per level exhausts Python's
recursion limit on such a file, so every walker here keeps its own stack.
"""

from contextlib import contextmanager


def iter_preorder(root, descend=None):
    """Yield *root* and every node under it, in document order, iteratively.

    The order is the one a recursive pre-order walk produces: a node before
    its children, and children left to right.

    Args:
        root: the tree-sitter node to start from (yielded first).
        descend: optional predicate; when it returns False for a node, that
            node is still yielded but nothing under it is visited.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if descend is None or descend(node):
            # Reversed, so the leftmost child is popped (visited) first.
            stack.extend(reversed(node.children))


class SourceParseError(Exception):
    """A source file could not be parsed or analyzed; the message names it."""

    def __init__(self, filepath, reason):
        self.filepath = filepath
        self.reason = reason
        super().__init__(f"{filepath}: could not be parsed: {reason}")


@contextmanager
def naming_source(filepath):
    """Re-raise any failure while parsing or walking *filepath* as a
    :class:`SourceParseError` that names the file.

    Without it, a failure deep inside a sweep over thousands of files surfaces
    as a bare exception ("maximum recursion depth exceeded") that says nothing
    about which file caused it.
    """
    try:
        yield
    except SourceParseError:
        raise
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        raise SourceParseError(filepath, f"{type(exc).__name__}: {reason}") from exc
