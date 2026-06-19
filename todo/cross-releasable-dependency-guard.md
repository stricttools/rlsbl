# Guard against externally-reachable cross-releasable dependencies

## Problem

Two releasables in a monorepo ship independently. If a package in releasable A has a runtime dependency on a package in releasable B, and an external consumer installs from releasable A, they can get a version of the releasable-B package that's out of sync with what releasable A was developed against.

Inside the monorepo, this is invisible — everything is at HEAD, tests pass, the skew never manifests. It only breaks when a consumer installs a specific version of releasable A and gets a different version of the cross-releasable dependency.

## Example

Releasables: `platform` (core, utils, db) and `sdk` (sdk-python, sdk-go). `sdk-python` depends on `utils` (from the `platform` releasable).

1. `sdk` ships v1.0.0. `sdk-python` works with `utils` at HEAD.
2. `platform` ships v2.0.0 with a breaking change to `utils`.
3. Monorepo tests pass — `sdk-python` is updated to work with the new `utils`.
4. But `sdk` hasn't released again. A consumer installing `sdk-python==1.0.0` gets `utils` v2.0.0 (or whatever pip resolves), which is incompatible.

This is the diamond dependency version skew problem — the exact problem monorepos are supposed to prevent, reintroduced by independent release schedules.

## When it's safe

Cross-releasable dependencies are safe when they're **leaf-internal**: no external consumer can transitively reach them. If releasable A depends on releasable B but only through packages that are never installed outside the monorepo's own venv, there's no skew risk.

## Proposed guard

A check (e.g., `cross-releasable-dependency`) that:

1. For each releasable, walks the dependency graph of its member packages
2. Identifies runtime dependencies that cross into a different releasable
3. Checks whether any package along that cross-releasable path is externally consumable (not private, or referenced by a consumer)
4. Errors if an externally-reachable path crosses releasable boundaries

The safe pattern: cross-releasable deps exist but are purely internal. The unsafe pattern: an external consumer can transitively pull packages from two independently-versioned releasables.

## Severity

Warning by default (cross-releasable deps are common and usually safe). Error when the path is externally reachable (private = false, or has a publish pipeline).

## Relationship to existing checks

Similar in spirit to `dev-only-boundary` (prevents runtime deps on dev-only projects). This would prevent externally-visible runtime deps across releasable boundaries.
