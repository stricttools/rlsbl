# Stale scaffold template and docs: retired command forms still being propagated

Filed 2026-08-03.

## Context

A fleet-wide validation of documented CLI invocations against current schemas (2026-08-03) found rlsbl's own scaffolding and docs teaching retired command forms.

## Problem

1. **The scaffold CLAUDE.md template emits `rlsbl release [patch|minor|major]`** — the bump type has moved to the release file (`.rlsbl/releases/unreleased.toml`); the bracket form is not a valid invocation of any current command. The line was found propagated at or near line 8 of ~34 downstream repos' CLAUDE.md files (all scaffold-cloned). One downstream copy also adds a nonexistent `--registry pypi`.
2. **rlsbl's own docs are stale in places**: `README.md:134` and `docs/scaffold.md:227` reference `rlsbl scaffold --force`, whose current flag surface differs (target/publish-mode/auto-commit/skip-shared/auto-tag). A fuller audit of `docs/` and README against the current schema is warranted while in there (removed flags like `--watch-async` and removed/renamed commands have historically lingered in prose).

## Work

- Fix the template once; the fleet fix is then a re-scaffold rollout (three-way merge preserves downstream customizations; the stale line is in the scaffold-managed region).
- Correct the two known doc sites; run a schema-validated sweep over `docs/**.md` + README for other stale invocations (fenced blocks first — highest confidence; inline prose flagged for manual review).
- Consider: the planned CLI-surface scan tooling automates exactly this class of check; until it exists, a one-off validation pass suffices — do not build interim machinery.

## Affected files

Scaffold template source for CLAUDE.md (under the scaffold data/templates), `README.md`, `docs/scaffold.md`, possibly other `docs/*.md`.

## Effort

S. Independent of everything else; can land immediately.
