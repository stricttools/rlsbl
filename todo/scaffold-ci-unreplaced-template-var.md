# scaffold ci.yml: unreplaced {{pypi.minRequiredPython}} template variable

## Problem

`rlsbl scaffold` generates `.github/workflows/ci.yml` with a comment on line 18:

```
# requires-python: >= {{pypi.minRequiredPython}}
```

This template variable is not replaced during scaffold. The value should come from `requires-python` in the project's `pyproject.toml` (e.g., `3.11`).

## Observed in

- toolstream project scaffolded with rlsbl 0.74.1
- Scaffold warning: `.github/workflows/ci.yml: unreplaced vars: pypi.minRequiredPython`

## Expected behavior

The comment should read `# requires-python: >= 3.11` (or whatever the project's `requires-python` value is) after scaffold runs.

## Impact

Low -- it's in a comment, not live code. But the scaffold warning is noisy and the comment is misleading.
