# Schema version stamping: make it correct, complete, and format-safe

Filed 2026-08-03. This is the root prerequisite for everything that consumes per-version CLI schemas (surface diffing, release gating, historical baselines).

## Context

For CLI-framework-based projects, the release flow regenerates `.strictcli/schema.json` and patches the new version into it (`rlsbl/commands/release/validate.py:1034-1088`, `_patch_schema_version` at `:1090-1123`, invoked from `commands/release/__init__.py:844`).

## Problem: the stamp is historically unreliable and partially unsupported

Audit findings (2026-08-03):

- This repo's own 213 tags: 15 schemas stamped correctly, 130 off-by-one (schema at tag N says N-1), 68 missing entirely. Another fleet repo's 62 tags: 0 correct (35 say `"dev"`, 27 missing). The stamping step is a recent addition; history predates it.
- Live working trees carry garbage versions from Go build info under `go run` (`(devel)`, `+dirty` pseudo-versions, empty string) — only the release-time patch corrects them, so any schema NOT written by the release flow is mis-versioned.
- **TypeScript/npm projects are unsupported**: `_schema_dump_command` (`validate.py:1024-1031`) raises on any language other than Python/Go, and `strictcli_detect.py:20-42` probes only `pyproject.toml` then `go.mod`. npm CLI apps get no dump and no stamp at all.
- `_patch_schema_version` round-trips through Python `json` (`:1101-1117`). Applied to a TS-generated schema this would destroy the deliberately-exact float/bigint token forms the TS writer emits. npm support cannot reuse the current patcher.

## Work

1. npm/TS detection (`strictcli_detect.py`: probe `package.json` bin/deps) and dump invocation.
2. Replace json-round-trip patching with a textual patch of the `version` value (format-preserving for all three emitters), keeping the atomic-write behavior (`:1111-1123`).
3. Keep the existing hard-error-over-stale guarantee (`:1051-1054`); extend it to the npm path.
4. Multi-schema guard: fail on ambiguous/duplicate schema locations in one project (a stray duplicate at a monorepo root from a wrong-cwd dump was observed in the fleet) rather than picking one.

## Historical tags: two options

- **A. Start clean at the next release** (each repo's next tag carries a correct stamp; consumers treat older tags' `version` field as untrusted and key on the tag name instead). Pros: zero rewriting; consumers need the tag-name rule anyway for the 68+27 missing files. Cons: the bad stamps stay in history forever.
- **B. Backfill/correct against tag names** (rewrite history or maintain a correction table). Pros: self-consistent artifacts. Cons: history rewriting is disproportionate for metadata; a correction table is a second source of truth.

A is recommended; consumers keying on tag names makes the in-file stamp a convenience, not an authority.

## Affected files

`rlsbl/strictcli_detect.py`, `rlsbl/commands/release/validate.py` (`_schema_dump_command`, `_run_strictcli_schema_dump`, `_patch_schema_version`), tests.

## Effort

M. Blocks: the CLI-surface release gate (`todo/cli-surface-release-gate.md`) and trustworthy git-tag baselines for surface diffing.
