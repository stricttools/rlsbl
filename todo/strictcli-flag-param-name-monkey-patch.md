# strictcli _flag_param_name monkey-patch

## Problem

rlsbl monkey-patches `strictcli._flag_param_name` at startup (`rlsbl/__init__.py` lines 21 and 31). It saves the original function, then replaces it with `_flag_param_name_with_kw_safety` which appends `_` to Python keyword names (like `global`). This is fragile because:

- It depends on an internal private function that could be renamed, moved, or removed
- If strictcli changes the function signature or behavior, rlsbl silently breaks
- Monkey-patching makes the dependency invisible to static analysis

## Possible fixes

- strictcli adds a public API for customizing flag parameter name conversion (a hook or option)
- strictcli handles Python keyword collision internally (making the monkey-patch unnecessary)
- rlsbl works around keyword collisions at its own layer without patching strictcli internals

## Notes

This is separate from the `run_checks` migration todo -- that one covers calling private functions, this one covers replacing private functions at runtime.

## Effort

Small to moderate depending on the approach. The strictcli-side fix (handling keywords internally) would be the cleanest.
