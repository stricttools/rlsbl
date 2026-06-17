# Mutable module-level state in __init__.py

## Problem

`rlsbl/__init__.py` has mutable module-level globals (`_variadic_args`, `_resolved_project`) that are mutated at runtime before strictcli parsing. This makes the CLI entry point stateful and harder to test or compose.

## Suggested approach

- Encapsulate the variadic arg extraction and project resolution into a pre-parse step that returns a result object
- Pass the result through to command handlers instead of reading from module globals
- Consider whether strictcli supports a pre-parse hook that could replace the current pattern

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), architecture debt section.
