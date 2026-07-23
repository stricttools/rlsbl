# Unify monorepo release models: single releasable model

Filed 2026-07-23 at explicit user direction, superseding
`todo/.obsolete/rename-implicit-explicit-mode.md` (the user rejected the rename:
don't rename the modes, eliminate the mode axis instead — later).

## Context

Monorepos currently run one of two release models:

- "implicit mode" (no `[[releasables]]`): each package independently versioned via its
  own `<path>/.rlsbl/` (config, changes/, version), released via `[packages.<name>]`
  sections, tagged `{name}@v*`.
- "explicit mode" (`[[releasables]]` present): packages grouped into releasable units
  sharing one version/changelog/tag under `.rlsbl-monorepo/releasables/<name>/`,
  released via `[releasables.<name>]` sections.

A releasable can contain exactly one package, so independent versioning is expressible
as N singleton releasables — one fleet monorepo already runs that shape in production.
Implicit mode is therefore a redundant expression of a degenerate releasable.

## Problem

The mode axis is a recurring bug habitat. Evidence from the 2026-07-22/23
investigation round alone:

- `monorepo absorb`'s changelog double-append is implicit-mode-only (empirically
  verified: duplicate lines AND duplicate entry ids in unreleased + finalized files).
- `monorepo cleanup` hard-refuses implicit-mode workspaces — the corruption above is
  unfixable by the blessed tool exactly where it occurs.
- The release file has two parallel schemas (`[packages.*]` vs `[releasables.*]`).
- Changelog-home resolution, coverage anchoring, and batch release logic all branch on
  `is_explicit_mode` (rlsbl/workspace.py) — historically the source of three-way
  disagreements that took dedicated fix campaigns.

Per the codified rule: when guardrails keep accumulating against the same bug class,
make the class structurally impossible. Killing the axis is that structural change.

## Decision (user, 2026-07-23)

No rename of the modes. Unify later on a single releasable model. This todo is the
"later".

## Scope sketch

- Delete `is_explicit_mode()` and every branch on it; one code path everywhere
  (checks/_common.py, changelog/home.py, release flows, batch plan, migrate/cleanup,
  tag_glob, absorb routing).
- Delete the `[packages.*]` release-file schema; `[releasables.*]` only. Breaking
  change (0.x, break cleanly, no shims — see CLAUDE_ADDITIONS backward-compat rule).
- `monorepo add` (and absorb) auto-scaffold a singleton releasable per new package;
  independent versioning = singleton releasables.
- Convert the two remaining implicit-mode monorepos in the fleet (their
  releasable-migration todos have been deferred repeatedly; this absorbs them).
  `migrate-releasable` already exists as the conversion tool.
- A planned multi-repo consolidation currently designed around implicit mode should be
  updated to N singleton releasables before it executes.
- `monorepo cleanup`'s implicit-mode refusal becomes moot; the vocabulary problem the
  rename todo described evaporates (there are no modes left to name).

## Sequencing

After the current cycle's absorb/dry-run/globals work ships. The absorb overhaul
should be designed compatibly (absorb always targets a releasable, auto-created
singleton by default) so this unification deletes code rather than reworking it.

## Effort estimate

Medium-large, deletion-shaped: the dual branches and special cases collapse; the two
fleet conversions ride along; plus docs/selfdoc regeneration and the workspace-level
CLAUDE.md rlsbl section update after release.
