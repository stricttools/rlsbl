# Adopt ecosystem schema validation for planted TOML files; close the 1.1 writer gap

## Context

The ecosystem has adopted a TOML usage profile (pinned to TOML 1.0 syntax)
with a validation-by-owner model: each tool that owns a TOML format ships
schemas for it and validates its own files by importing the ecosystem's
schema-validation engine. rlsbl is the largest format owner — it plants TOML
in nearly every repo (release files, finalized per-version archives, lint
configs, workspace files, batch-exclusion records, dead-module lists,
action-version pins) and already maintains schemas for several of these
(release-file, config, changelog-entry).

Separately, an empirically verified skew problem: rlsbl's round-trip TOML
editing dependency (tomlkit >= 0.12) is TOML 1.1-compliant and silently
accepts 1.1-only syntax (multi-line inline tables, trailing commas,
`\xHH`/`\e` escapes, secondless datetimes), while stdlib `tomllib` and the
ecosystem's Go-side parser are strictly 1.0 and hard-fail on it. rlsbl's
write path is therefore the one place in the ecosystem where 1.1 syntax can
enter files that other tools then reject.

Decision provenance: validation-by-owner via the shared engine was a
deliberate user decision; per the ecosystem's no-interim-mitigation rule, the
1.1 gap gets no standalone shim — the profile's 1.0 syntax check, applied to
rlsbl's own written files, is the fix.

## Problem

1. **Complete the schema set.** Schemas exist for release-file, config, and
   changelog-entry formats. Missing: lint configs (`.rlsbl/lint/*.toml`),
   `workspace.toml`, batch-exclusion records (`batch-*.toml`),
   `dead-modules.toml`, `action_versions.toml`.
2. **Validate own files via the shared engine.** Replace hand-rolled
   validation of rlsbl-owned TOML with the ecosystem schema-validation
   engine's Python target, so semantics match the schemas exactly and error
   codes are uniform. Gate: verify the engine's Python target maturity first
   (its conformance status), before switching — no adoption of a validator
   that does not fully execute its own corpus.
3. **Validate at write time, including the 1.0 syntax check.** Every TOML
   file rlsbl writes or edits gets validated (schema + 1.0-syntax pin)
   before the write is committed to disk. This closes the tomlkit 1.1 hole
   structurally: non-1.0 syntax becomes a hard error at the point of
   introduction, regardless of which library wrote the bytes. Finalized
   per-version archives get validated at generation time (they are chmod 444
   afterward).
4. **Claim ownership in repo manifests.** Under the coverage model, rlsbl
   declares itself the owning validator for the files it plants, so repo
   coverage checks can attribute them.

## Solutions

**Option A — engine adoption and write-time validation together (decided
direction).** Pros: one pass over the write paths; the syntax pin arrives
with the same mechanism that will enforce it long-term. Cons: blocked on
engine target maturity verification.

**Option B — write-time 1.0 syntax check first, engine adoption second.**
Pros: closes the skew hole sooner. Cons: builds a temporary validation path
that the engine adoption then replaces — exactly the interim-shim pattern the
ecosystem forbids; rejected.

## Affected files

- Schema files for the five missing formats (ship in this repo)
- Write/edit paths that touch TOML (centralize on a single validated-write
  helper if not already centralized)
- Release flow step that generates finalized version archives
- Scaffold templates, if manifests/ownership declarations are planted per repo

## Effort

Schemas: small each, five of them. Engine adoption + validated-write helper:
medium. Sequencing dependency: the shared engine's 1.0 syntax check and its
Python target maturity.
