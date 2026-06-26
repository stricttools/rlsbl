# Unreachable-code detection false positive on conditional returns

## Problem

The `unreachable-code` lint rule flags `return` statements inside `if` blocks as making subsequent code unreachable. This is incorrect — code after an `if` block containing a `return` IS reachable (when the condition is false).

Example from `authbl/src/authbl/auth.py`:

```python
if alg != "RS256":
    return None           # <-- lint flags line 231: "unreachable code after return"

kid = header.get("kid")   # <-- this IS reachable (when alg == "RS256")
```

The pattern repeats for 3 more conditional returns at lines 254, 258, 262 — all inside `if` blocks with code continuing after.

## Impact

4 false positive errors block the auth releasable release. The code is correct — these are early-return guard clauses, a standard Python pattern.

## Root cause

The unreachable-code detection (added in 0.86.0) likely treats any `return` statement as making the next line unreachable, without checking whether the `return` is inside a conditional branch. Only unconditional returns (not inside `if`/`elif`/`else`) should trigger this rule.

## Affected files

- `rlsbl/lint/` — unreachable-code detection logic (tree-sitter based)
