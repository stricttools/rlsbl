# ReleaseConfig should be a dataclass

## Problem

`ReleaseConfig` is currently a raw tomlkit dict with no validation. Fields are accessed by string key with no type safety. Invalid or missing fields are caught late during release execution rather than at parse time.

## Suggested approach

- Convert to a `@dataclass` with typed fields and `__post_init__` validation
- Parse and validate at TOML load time, not at consumption time
- Ensure all consumers use typed field access instead of dict key access

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), architecture debt section.
