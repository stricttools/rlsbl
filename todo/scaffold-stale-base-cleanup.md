# Scaffold: clean up stale base files when managed files are removed

## Problem

When `rlsbl scaffold` removes a managed file (e.g., because the feature that generated it was deprecated), it correctly:
- Deletes the managed file from disk (if unmodified from template)
- Removes it from `managed-files.json`

But it does NOT clean up the corresponding base file in `.rlsbl/bases/`. Base files exist solely for three-way merging of scaffold-managed files. When a managed file is removed, its base file becomes an orphan with no purpose.

## Observed case

When hook scripts were deprecated in favor of config-driven hooks (v0.81.0), re-scaffolding removed `.rlsbl/hooks/pre-checks.sh` and `.rlsbl/hooks/pre-release.sh` from managed files. But `.rlsbl/bases/.rlsbl/hooks/pre-checks.sh` and `.rlsbl/bases/.rlsbl/hooks/pre-release.sh` were left behind as stale orphans. Discovered in a downstream project after re-scaffolding from 0.88.1 to 0.96.0.

## Fix

When scaffold removes a file from `managed-files.json`, also delete its corresponding entry under `.rlsbl/bases/`. The base path is deterministic: `.rlsbl/bases/<managed-file-path>`.

## Affected files

- Scaffold logic that handles managed file removal/deprecation
- Possibly a one-time cleanup pass for existing projects with orphaned bases
