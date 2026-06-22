# Monorepo scaffolding: remaining overhead

Split from `monorepo-scaffolding-overhead.md`. Config inheritance, config-driven hooks, and per-package artifact cleanup are done. Two items remain:

## 1. Bulk re-scaffold with releasable scope

`rlsbl scaffold` operates on one package at a time. A `--releasable <name>` flag (or `rlsbl monorepo scaffold`) would re-scaffold all packages in a releasable in one operation. This doesn't reduce the file count but reduces manual effort and ensures consistency.

## 2. CI workflow templates at releasable level

Per-package CI workflows are the biggest remaining source of scaffold churn. Options (deferred by design decision):
- One CI workflow per releasable with a matrix over member packages
- Per-package workflows generated from a single releasable-level template

The matrix approach has limits (GitHub Actions 256-job limit, mixed ecosystems can't share a matrix). Generating per-package workflows from a releasable template reduces the source-of-truth duplication without these constraints.
