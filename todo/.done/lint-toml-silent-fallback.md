# Lint TOML config: silent fallback on parse failure

## Context

Found during a code audit (2026-07) of the lint configuration path.

## Problem

When the lint-related TOML configuration fails to parse (or is missing where expected), the
code silently falls back to defaults instead of hard-erroring. A malformed config therefore
behaves exactly like no config: the user's declared lint intent is discarded without any
signal. This is the banned silent-degradation pattern — "try the config, fall back if it
fails."

## Suggested fix

1. Red test: a syntactically invalid lint TOML must abort with a parse error pointing at the
   file, never proceed with defaults.
2. Separate the two cases explicitly: file absent = documented default behavior (explicit mode
   selection by absence is fine); file present but unreadable/invalid = hard error, always.
3. Check the other config-file loaders for the same try/except-fallback shape while in there.

## Affected area

Lint configuration loading.

## Effort

Small (error propagation + tests).
