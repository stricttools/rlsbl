# push_timeout in config.json not found due to relative path resolution

## Problem

`get_push_timeout()` reads `push_timeout` from `.rlsbl/config.json` via `read_project_config()`, which uses `_project_config()` returning the relative path `".rlsbl/config.json"`. During `rlsbl release run`, the CWD may shift (e.g., from the sub-project directory to the repo root for git operations). When `get_push_timeout()` is called after a CWD change, the relative path doesn't resolve, `read_json_config` silently returns `{}` (catches `OSError`), and the function raises "push_timeout not configured" even though the config file exists and has the value.

## Reproduction

```bash
cd /home/m/Projects/strictcli/python
cat .rlsbl/config.json   # has "push_timeout": 300
rlsbl release run --watch --yes
# Error: push_timeout not configured.
```

Works with env var: `RLSBL_PUSH_TIMEOUT=300 rlsbl release run --watch --yes`

## Root cause

`rlsbl/config.py:34-36`:
```python
def _project_config():
    return os.path.join(".rlsbl", "config.json")  # relative to CWD at call time
```

This is called lazily at read time, not resolved to an absolute path at startup.

## Fix

Resolve the project config path to an absolute path once at the start of the release flow, or have `_project_config()` resolve relative to the detected project root rather than CWD.
