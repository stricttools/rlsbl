# Detect VERSION constant divergence

## Problem

A project's in-source version constant (e.g., `VERSION = "0.31.0"` in a TypeScript `index.ts`) can drift from the canonical version file (e.g., `package.json` says `"0.33.0"`). rlsbl bumps the version file during release but may miss hardcoded version strings elsewhere in the source.

## Solution

Add a preflight check during `rlsbl release run` that scans for common version constant patterns in source files and hard-errors if any diverge from the version being released. Patterns to detect:

- Python: `__version__ = "..."`, `VERSION = "..."`
- TypeScript/JavaScript: `const VERSION = "..."`, `export const VERSION = "..."`
- Go: `const Version = "..."`, `var Version = "..."`

This should be a hard error, not a warning. If a project has version constants that intentionally differ (e.g., a protocol version), they should be excluded via config.

## Effort

Small -- pattern matching + string comparison during preflight.
