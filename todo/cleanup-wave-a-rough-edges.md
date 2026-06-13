# Clean up Wave A rough edges

## Context

A batch of seven fixes was committed to rlsbl by a session that launched implementation without approval. The fixes themselves are sound (35+ new tests, red-green TDD, 3453 suite passing), but a concurrent session's review flagged three rough edges that should be cleaned up.

## Items

### 1. Bare `except Exception` in undo.py release-file restoration

The new "Restore release file" step in `rlsbl/commands/undo.py` (added by commit `e1f17c1`) uses a bare `except Exception` that swallows errors without logging tracebacks. The existing undo steps follow the `(step_name, status, remediation)` pattern with continue-on-failure, which is correct, but the except block should log the traceback (e.g., `traceback.print_exc()` or at minimum `print(str(e), file=sys.stderr)`) so the user can diagnose what went wrong. Match the error reporting style of the surrounding steps in the same function.

### 2. Shell-echo changelog entry misclassified as fix

Commit `c10e5c2` added command-echo output before deploy local steps and custom asset builds. The changelog entry was typed as `--type fix`, but adding new visible output is a feature, not a bug fix. Amend the entry: `rlsbl changelog amend` to change the type from `fix` to `feature`. The description text is fine as-is.

### 3. Verify no other rough edges

Grep the seven commits for patterns that don't match house style:
- Any other bare `except Exception` or `except:` without logging
- Any `sys.exit()` calls added in library code (the codebase has 34 existing ones being tracked for removal, but new ones should not be added)
- Any missing type hints on new public functions
- Any tests that assert too little (e.g., only checking exit code)

Fix anything found; report anything debatable.

## Scope

Small. Traceback logging in one except block, one changelog amend, one audit pass.
