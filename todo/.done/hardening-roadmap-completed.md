# rlsbl hardening roadmap -- completed items

## Context

A thorough seven-dimension investigation (architecture, release pipeline failure modes, test coverage, changelog/checks, scaffold/monorepo, bug history, consumer fleet) was conducted on v0.69.2/v0.70.0. This todo consolidates the findings into a prioritized work plan. The investigation conversation has full details including file:line references, concrete failure scenarios, and ranked work items per dimension.

These items were completed in v0.71.0-v0.72.0.

## Pipeline robustness gaps (completed)

- **Tag push after commit push (release.py ~1507-1547):** tag push is OUTSIDE the try-catch that handles rollback. If commit pushes but tag push fails, commit is public, no tag, no rollback runs. Fix: wrap both pushes in one try block.

## Suggested execution order (completed items)

1. Retire the superseded scaffold-conflict-markers check (separate todo exists)
2. Clean up Wave A rough edges (separate todo exists)
