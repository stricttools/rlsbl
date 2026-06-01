# publish templates lack working-directory for multi-target

## Problem

When rlsbl config has npm/pypi targets with `path` (e.g., `{"name": "npm", "path": "npm/"}`), the merged `publish.yml` doesn't set `working-directory` on the npm/pypi jobs. They run `npm publish` and `uv build` from the repo root where no `package.json`/`pyproject.toml` exists, so publishing fails.

## Affected code

`_generate_merged_publish()` in `commands/init_cmd.py` computes `target_paths` but never injects them into the workflow templates.

## Fix

Pass `target_paths` into template rendering and use `working-directory: {{path}}` in the npm and pypi publish job templates.
