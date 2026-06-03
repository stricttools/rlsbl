# release retry validation failure does not clean up retry.toml

## Problem

The v0.56.0 changelog says: "Fix retry.toml blocking release. `release retry` now cleans up the scaffolded retry.toml when validation fails." But the actual code at `rlsbl/__init__.py:300-302` catches the `ValueError` from `read_retry_file` and exits without deleting the file. The `_cleanup_retry_file` function in `commands/release_retry.py:95` exists but is only called after a successful retry, not on validation failure.

## Steps to reproduce

1. Have a pre-existing `.rlsbl/releases/retry.toml` with `ref = ""` (auto-scaffolded)
2. Run `rlsbl release retry --watch --yes`
3. Get error: `Error in retry file: ref must be set in retry.toml`
4. File still exists
5. `rlsbl release run --watch --yes` fails with "working tree is not clean"

## Expected behavior

When `read_retry_file` raises `ValueError` (validation failure), the handler at `__init__.py:300-302` should delete the retry.toml file before exiting, matching the changelog claim.

## Fix

Add cleanup between the error print and `sys.exit(1)`:
```python
except ValueError as e:
    print(f"Error in retry file: {e}", file=sys.stderr)
    _cleanup_retry_file(retry_path)  # <-- missing
    sys.exit(1)
```

Or better: import and call `_cleanup_retry_file` from `commands/release_retry.py`.
