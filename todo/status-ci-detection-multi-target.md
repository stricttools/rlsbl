# `rlsbl status` reports "CI: missing" for multi-registry scaffolds

## Context

Multi-registry standalone projects (e.g. a repo with `targets: ["npm", {"name": "pypi", "path": "pypi"}]`) get per-target CI workflows from `rlsbl scaffold`: `.github/workflows/ci-npm.yml` and `.github/workflows/ci-pypi.yml`. No single `ci.yml` is generated in this layout (observed with rlsbl 0.110.2).

## Problem

`rlsbl status` hardcodes the path `.github/workflows/ci.yml` when reporting CI presence. On a multi-registry scaffold it therefore prints `CI: missing` even though both per-target CI workflows exist and are exactly what scaffold itself generated. Cosmetic, but it makes a correctly-scaffolded project look broken and trains agents/users to distrust the status output.

## Possible solutions

1. **Derive expected workflow filenames from the configured targets** (`ci.yml` for single-target, `ci-<target>.yml` per target for multi-target) and report each. Pro: status matches scaffold's own naming logic, per-target granularity (`CI: npm ok, pypi ok`). Con: duplicates scaffold's naming knowledge unless factored into a shared helper.
2. **Glob for `ci*.yml` in `.github/workflows/`**. Pro: trivial. Con: loses the "which target" signal and could match unrelated files.
3. Factor a `expected_ci_workflows(config)` helper used by both scaffold and status (variant of 1). Pro: single source of truth, immune to future naming drift. Con: slightly larger refactor.

Option 3 is the most correct; option 1 is acceptable if the helper already effectively exists in scaffold code.

## Affected files

- `rlsbl status` implementation (the CI-presence check)
- possibly the scaffold template-naming code, if factoring a shared helper (option 3)

## Effort

Small — likely under an hour including a regression test (status on a two-target fixture should report both workflows).
