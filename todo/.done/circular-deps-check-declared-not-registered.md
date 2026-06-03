# circular-deps check declared in checks.toml but not registered

## Problem

`rlsbl/data/checks.toml` line 303 declares `[checks.circular-deps]` but the corresponding check function is never registered with strictcli. Running `rlsbl check --all` in any project fails with:

```
error: checks declared in checks.toml but not registered: circular-deps
```

This blocks `rlsbl release run` because the release flow runs checks.

## Impact

Blocks gamehome v0.2.8 release. All projects using rlsbl 0.57.x are affected.

## Context

The `find_circular_deps` function exists in `dep_validation.py` but is not wired as a strictcli check. Either the check declaration in checks.toml is premature (the check isn't implemented yet) or the registration was missed.
