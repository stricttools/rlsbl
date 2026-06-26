# Unreachable-code lint false positive on inline comments

## Problem

`_check_unreachable_code` in the Python AST checker treats tree-sitter `comment` nodes as named block children. When an inline comment appears on the same line as a `return` or `raise` statement, the comment is parsed as a subsequent named child of the block, triggering a false-positive "unreachable code after return" error.

## Steps to reproduce

A Python file containing:

```python
def foo() -> int:
    return bar()  # type: ignore[return-value]
```

The lint flags the `# type: ignore[return-value]` comment as unreachable code after the `return` statement.

This pattern is common with mypy type-ignore directives, but any inline comment on a terminal statement triggers the same false positive.

## Expected behavior

Inline comments on the same line as a terminal statement (`return`, `raise`, `break`, `continue`) should not trigger unreachable-code detection.

## Fix

Skip `comment` nodes when iterating `named_children` in `_check_unreachable_code`. Comments are not executable code and should never be considered reachable or unreachable.

## Context

Discovered during orxtra v0.7.0 release. The lint blocked the release until the affected line was rewritten to avoid the inline comment.
