# Release lint ignores lint_allow from workspace.toml

## Problem

`run_releasable_lint()` in `rlsbl/commands/release/hooks.py:616` passes `allowed_imports=None` to `_run_builtin_lint()`, ignoring the `lint_allow` field from workspace.toml project entries.

The check system (`rlsbl/checks/quality.py:35`) correctly reads `proj.get("lint_allow")` and passes it through. But the release flow doesn't — it only reads `proj.library` from `ws_projects`, not `lint_allow`.

## Impact

Library packages with `lint_allow` in workspace.toml pass `rlsbl check --tag quality` but fail during `rlsbl monorepo release run`. The lint exception is recognized by checks but not by releases.

Example: auth-sdk-go has `lint_allow = ["net/http"]` in workspace.toml. `rlsbl check` passes. `rlsbl monorepo release run` fails with 4 `forbidden-import` errors for `net/http`.

## Fix

Line 616 in hooks.py should read `lint_allow` from the workspace project entry (the same way it reads `library`) and pass it as `allowed_imports`:

```python
# Build a lookup from name to (library, lint_allow)
for proj in ws_projects:
    lib_lookup[pkg_name] = (proj.library, proj.get("lint_allow"))

# ...
is_library, allowed = lib_lookup.get(pkg_name, (False, None))
if is_library:
    _run_builtin_lint(flags, ..., allowed_imports=allowed, ...)
```

## Affected files

- `rlsbl/commands/release/hooks.py` — `run_releasable_lint()`, line 616
