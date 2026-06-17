# Monorepo batch partial failure recovery

## Problem

When `rlsbl monorepo release` releases packages 1 through N and package K fails mid-batch, packages 1 through K-1 are fully released with no coordinated undo. There is no mechanism to resume from package K or roll back the entire batch.

## Suggested approach

- Record released packages in a batch state file during execution
- Provide `rlsbl monorepo release --resume` to continue from where it left off
- Provide `rlsbl monorepo release --undo` for coordinated batch rollback
- Consider whether partial releases should be blocked entirely (all-or-nothing)

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), pipeline robustness section.
