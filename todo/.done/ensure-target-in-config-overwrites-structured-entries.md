# Bug: `_ensure_target_in_config` overwrites structured target entries with plain strings

## Problem

`_ensure_target_in_config` in `init_cmd.py` reads targets from `ctx.config` (the in-memory `ProjectContext.config` dict) rather than from the on-disk config file. When `ctx.config` is `{}` (a bare `ProjectContext` with no loaded config), the function proceeds to overwrite the disk config's `targets` key with whatever it computes from the empty in-memory state.

This destroys structured target entries. For example, a config file containing:

```json
{"targets": [{"name": "go", "path": "go/"}]}
```

gets overwritten with:

```json
{"targets": ["go"]}
```

The subdirectory `path` information is lost.

## Impact

- Pre-existing bug; triggers whenever `ctx.config` is empty (`{}`).
- Known trigger: test scenarios that construct a bare `ProjectContext`.
- Potential trigger: bare CLI invocations where config loading is skipped or fails silently.
- Downstream effect: scaffold, release, and CI workflows that depend on subdirectory paths in target entries will use the wrong working directory.

## Fix direction

- Read targets from the on-disk config file (not `ctx.config`) before deciding whether to overwrite.
- Alternatively, validate that `ctx.config` is fully populated before allowing it to overwrite disk state -- error if it is empty rather than silently clobbering.
- The function should never downgrade a structured entry to a plain string.
