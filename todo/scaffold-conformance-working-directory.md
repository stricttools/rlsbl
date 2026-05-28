# Scaffold sets wrong working-directory for cross-project CI jobs

## Problem

When a monorepo has a conformance sub-project that validates other sub-projects, the scaffolded CI workflow inherits the wrong working directory. In strictcli, the conformance CI job ran with `working-directory: python/` (inherited from the Python project's workflow template), but the conformance scripts resolve fixture paths relative to `conformance/`. This caused CI failures that passed locally.

## What happened

The conformance test cases used relative paths (`@fixtures/simple.txt`). The runner resolved them relative to CWD. Locally, CWD was `conformance/` (correct). In CI, CWD was `python/` (wrong) because the scaffold template set it.

## Suggestion

Monorepo conformance/integration CI jobs should either:
1. Get their own `working-directory` in the workflow template
2. Or the scaffold should detect when a job is cross-project and not inherit the parent project's working directory

## Current workaround

The strictcli conformance CI workflow was manually rewritten to install the conformance tool and run `conformance check --tag pre-release` from the `conformance/` directory, bypassing the scaffold template entirely.
