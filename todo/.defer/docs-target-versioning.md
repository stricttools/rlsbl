# Docs target versioning

## Problem

`DocsTarget.read_version()` hardcodes `0.0.0`, which causes the version-consistency check to fail. Unlike secondary targets that shadow the primary target's version, docs is a peer target -- it deserves its own independent version tracking.

## Context

In the monorepo versioning design, the docs site is the N+1th project alongside the other sub-projects, each with its own version. The docs target currently cannot participate in version consistency checks because it has no real version source -- it always returns `0.0.0`, which is never consistent with anything.

## Constraint

Monorepo sub-projects version independently. The docs target needs to follow this pattern rather than inheriting a version from another target or hardcoding a placeholder.

## Proposed approach

The docs target should read its version from a real source:

1. Read from `selfdoc.json` if it has a `version` field (most explicit).
2. Alternatively, read from a dedicated `version` field in the docs target config within `workspace.toml`.
3. During `rlsbl release`, bump the docs target version alongside other targets when the docs site is part of the release.

This makes the docs target a proper versioned project that passes consistency checks without special-casing.

## Affected files

- `rlsbl/targets/docs.py` -- `DocsTarget.read_version()` and `write_version()` implementations
- `rlsbl/targets/docs.py` -- version consistency check logic (if docs is excluded via special-case, remove that)
- Monorepo `workspace.toml` -- may need a `version` field in the docs target section
- Target project's `selfdoc.json` -- may need a `version` field

## Effort

Small -- the version read/write methods are straightforward, and the release pipeline already handles per-target versioning.
