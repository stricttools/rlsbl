# Batch changelog coverage for monorepo ecosystems

## Problem

Cross-ecosystem maintenance (e.g., updating selfdoc.json across 17 projects) creates commits in many repos that each need changelog coverage. Running `rlsbl changelog add --no-user-facing` per-commit per-project is mechanical tedium that consumed significant time across multiple sessions.

## Proposed solution

A `rlsbl monorepo changelog` (or `rlsbl changelog batch`) command that:
1. Iterates all workspace projects (or all projects in a directory)
2. Runs changelog coverage check for each
3. Auto-adds `--no-user-facing` entries for uncovered housekeeping commits
4. Optionally prompts for user-facing entries when heuristics suggest real changes

## Context

The existing `rlsbl monorepo release` handles batch releasing. A companion batch changelog command would complete the workflow.
