# Releasable coverage check ignores JSONL entries

## Problem

`rlsbl check --tag changelog` reports commits as "uncovered" even when they are present in JSONL entries (both `unreleased.jsonl` and released version files like `0.1.0.jsonl`).

Reproduction:
- A monorepo with a `[[releasables]]` section in `workspace.toml`
- JSONL files under `.rlsbl-monorepo/releasables/<name>/changes/`
- Entries in `unreleased.jsonl` with full 40-char commit hashes
- `rlsbl check --tag changelog` still reports those exact hashes as "unreleased commit not covered"

The coverage check appears to not be reading the releasable's JSONL files, or is reading them but not matching hashes correctly. All other changelog checks pass (hash resolution, schema, orphans, range, batch limits).

## Context

468 commits reported as uncovered despite all being present in JSONL entries. The commits span the full releasable history (from the consolidation tag to HEAD). Clearing the `.validated` cache does not help.

The releasable was created via consolidation from 16 per-sub-project changelogs. The consolidated entries in `0.1.0.jsonl` through `0.2.0.jsonl` were created during that process. New entries in `unreleased.jsonl` were added via `rlsbl changelog add` from a sub-project directory.

## Possible causes

1. The coverage scanner may be looking at per-sub-project JSONL paths (`.rlsbl/changes/`) instead of the releasable path (`.rlsbl-monorepo/releasables/<name>/changes/`)
2. The hash matching may be using a different resolution than what's stored
3. The range calculation may be using a different tag than expected for the releasable

## Impact

Blocks releases — `rlsbl release run` validates changelog coverage and will fail. Workaround: unknown, all hashes are already covered.
