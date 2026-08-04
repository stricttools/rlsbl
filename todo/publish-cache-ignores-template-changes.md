# Publish-router cache does not invalidate on gate/template changes

## Context

`monorepo sync` skips publish-router regeneration when
`.rlsbl-monorepo/publish-cache.json` says it is up to date. The cache key
covers the rlsbl version and member workflow hashes — but NOT the gate/router
template content itself.

## Problem

When rlsbl's gate builder changes without a version bump (e.g. an editable
install carrying a just-committed fix, or any template change within one
version), `monorepo sync` reports "Publish router up to date, skipping
regeneration" and consumers keep the stale generated workflow. This bit twice
in one day: a gate env fix (GH_REPO) and a member-job restructuring both
required manually deleting the cache file to force regeneration.

## Expected

The cache key should incorporate a hash of the template/generator inputs that
actually determine the output (gate job builder output for the repo's shape,
router templates), so any change that would alter the generated workflow
invalidates the cache. Deleting the cache by hand must never be part of the
workflow.

## Effort

S–M: extend the cache key computation + a red-green test (template change →
sync regenerates; no change → sync skips).
