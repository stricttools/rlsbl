# CHANGELOG generation crashes with a raw traceback if a per-version .md is read-only

## Context

Finalized `.rlsbl/changes/<version>.jsonl` files are chmod 444 by design. The per-version `.md` files next to them are regenerated in place by CHANGELOG generation on every release.

## Problem

If a per-version `.md` file is read-only (a plausible state — a user or tool reasonably assumes the finalized pair jsonl+md are both immutable), `release run` hard-crashes mid-flight with a raw `[Errno 13] Permission denied` traceback during CHANGELOG generation — no friendly error, and the failure lands deep in the release (after tests/scans), triggering the abort path. Observed in a consumer project.

## Solution

Two options (both defensible):

1. Treat `.md` regeneration idempotently: if the target is read-only, compare content; if identical, skip; if different, error with a clear message naming the file and the expected permission model.
2. Preflight check: before the pipeline runs, verify every file the release will write is writable; hard-error with the list up front.

Either way, document the intended permission model for `<version>.md` (writable, regenerated) vs `<version>.jsonl` (444, immutable) — the asymmetry is currently implicit.

## Affected files

- CHANGELOG/markdown generation under `rlsbl/`
- Release preflight
- Docs describing the changes/ directory layout

## Effort

Small.
