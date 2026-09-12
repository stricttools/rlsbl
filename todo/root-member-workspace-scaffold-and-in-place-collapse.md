# A workspace whose only member is the repository root: scaffold skips it, and nothing collapses it to standalone in place

## Context

A workspace may declare the repository root (`path = "."`) as a releasable
member. A repository that started as a multi-package workspace and then merged
everything into one root-level module ends up in that shape: one releasable,
one member, the root.

## Problem

Two gaps, found while releasing such a repository:

- `rlsbl scaffold` returns early at a workspace root ("use rlsbl monorepo sync
  instead"), so a releasable root member is never scaffolded. Its goreleaser
  config, CI workflow and any launcher shims had to be rendered by hand from
  the templates. The early return predates root members; a releasable root
  member is a package and should be scaffolded like any other, with its
  managed-files record and bases under the releasable's state directory rather
  than a root `.rlsbl/`, which `root-rlsbl-conflict` refuses. Relocating the
  scaffolding version marker needs a decision, since `<releasable>/version` is
  the project version.
- There is no command that converts such a workspace into a standalone
  repository in place. `rlsbl monorepo extract` moves a releasable into a new
  repository and refuses one owning the root. The conversion was done by hand:
  move the releasable's `changes/`, `releases/`, `hooks/`, `config.json` and
  `transitions.jsonl` into `.rlsbl/`, write `.rlsbl/releasable.toml`, translate
  workspace-only config keys, park the other releasables' closed records
  somewhere rlsbl does not scan, delete `.rlsbl-monorepo/`, re-scaffold.

## Solutions

1. `rlsbl monorepo collapse`: a verified reconciler that performs the in-place
   conversion above, refuses when more than one member or more than one live
   releasable exists, records the closed releasables' histories in a location
   it documents, and previews under `--dry-run`. Recommended; it makes the
   conversion auditable and reversible.
2. Lift the scaffold early return for a releasable root member, resolving the
   managed-files record, bases, config and hooks through the releasable state
   directory, after deciding what to do with the scaffolding version marker.
3. Leave both as documented manual procedures.

## Affected files

`commands/init_cmd.py` (the early return and the CWD-relative `MANAGED_FILES`
and `BASES_DIR`), the monorepo command group, `releasable_cleanup.py`, the
workspace loader, docs for conversions.

## Effort

Medium for 1, small-to-medium for 2 depending on the marker decision.
