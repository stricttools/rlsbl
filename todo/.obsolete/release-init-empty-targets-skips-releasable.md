# release init skips releasables with empty targets list

## Problem

`rlsbl monorepo release init` fails for private releasables that have `"targets": []` in their config:

```
Warning: no targets detected for releasable 'X', skipping.
Error: no eligible releasables with detected targets.
```

The user must manually write the release TOML file every time, including an `include = [...]` field to specify targets.

## Root cause

`collect_releasable_targets()` in `rlsbl/targets/__init__.py` (line 250-251) returns `[]` immediately when the config has `"targets": []`, without falling through to member-level auto-detection:

```python
rel_targets = rel_config.get("targets")
if rel_targets is not None and isinstance(rel_targets, list):
    return list(rel_targets)  # Returns [] -- no fallback
```

Compare with `validate_release_targets` in `commands/release/validate.py` (lines 65-75), which correctly handles the same case: when the releasable-level result is empty, it falls through to scan member packages for their ecosystem manifests (package.json, pyproject.toml, go.mod, build.zig).

The two functions handle `"targets": []` inconsistently.

## Fix

Add a truthiness check to the short-circuit condition:

```python
if rel_targets is not None and isinstance(rel_targets, list) and rel_targets:
    return list(rel_targets)
```

This makes `"targets": []` mean "no explicit targets, auto-detect from members" (matching `validate_release_targets` behavior), while `"targets": ["npm", "pypi"]` still short-circuits with the explicit list.

## Context

Private repos set `"targets": []` to mean "don't publish to any registry." But they still cut releases (for tagging, changelog finalization, deploy triggering). The `private: true` flag already correctly suppresses manifest-bumping and publishing during `release run` — the empty targets list shouldn't also prevent `release init` from scaffolding.
