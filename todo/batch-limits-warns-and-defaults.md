# `batch_limits` warns-and-defaults instead of hard-erroring

## Context

Found during a code audit (2026-07) of config handling.

## Problem

When the `batch_limits` configuration is invalid or malformed, the code emits a warning and
continues with default values instead of failing. This is the warn-and-continue pattern the
ecosystem bans: agents ignore warnings, so an invalid config silently behaves as if it said
something else — the same input class produces behavior the author never declared.

## Suggested fix

1. Red test: an invalid `batch_limits` value must abort the operation with a hard error naming
   the key, the invalid value, and the accepted shape.
2. Replace the warn-and-default branch with the hard error. A missing key keeping its
   documented default is fine (that is the declared default); an *invalid present* value must
   never be silently replaced.
3. Grep for the same pattern (`warn` + `default` in config loading) — this is a bug class, not
   a single bug.

## Affected area

Config loading for `batch_limits` (the max-commits-per-entry / max-entries-per-commit limits).

## Effort

Small (branch swap + tests); the pattern grep is the lasting value.
