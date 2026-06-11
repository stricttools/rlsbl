# Migrate off strictcli private check API

## Problem

rlsbl's `pre_push_check.py` (lines 346-354) calls four private strictcli functions directly:

- `strictcli._filter_checks(app._check_defs, "prepush", None, False)`
- `strictcli._resolve_check_order(app._check_defs, selected)`
- `strictcli._run_checks(app, order, ctx, True)`
- `strictcli._check_format_human(results, False)`

It also accesses `app._check_defs` (a private instance attribute). These are undocumented internals that could break on any strictcli update.

Tests also depend on private API:
- `test_prepush_checks.py` (lines 276-312) calls `_filter_checks`, `_resolve_check_order`, `_run_checks` directly
- `test_prepush_checks.py` (lines 581-646) patches `strictcli._run_checks` and `strictcli._check_format_human`

## Solution

strictcli is adding a public API for running checks programmatically:

- `app.run_checks(context, *, tag_expr=None, name_glob=None, run_all=False, ignore_warnings=False)` returns `(list[CheckRunResult], int)`
- `strictcli.format_check_results(results, verbose=False)` returns `str`
- `strictcli.format_check_results_json(results)` returns `str`

### Production code migration

Replace `pre_push_check.py` lines 346-354 with:

```python
results, exit_code = app.run_checks(ctx, tag_expr="prepush", ignore_warnings=True)
print(strictcli.format_check_results(results))
sys.exit(exit_code)
```

No more `app._check_defs` access. No more private function imports.

### Test migration

- The full-pipeline test (lines 276-312) should call `app.run_checks()` instead of the three private functions.
- The mock test (lines 581-646) patches module-level functions (`strictcli._run_checks`, `strictcli._check_format_human`). After migration, this becomes a patch on `app.run_checks` (instance method -- different mock pattern) and `strictcli.format_check_results` (module-level, same pattern).

## Blocked on

strictcli Python release containing the public check runner API (`run_checks`, `format_check_results`, `format_check_results_json`, `CheckRunResult`).

## Effort

Small. Production code is a 4-line replacement. Test migration is moderate -- mock patterns change from patching module-level functions to patching an instance method.
