# deps-undeclared/deps-unused checks fail on packages where import name differs from package name

## Problem

The `deps-undeclared` and `deps-unused` checks match declared package names against import names found in source files. When a package installs under a different import name than its package name, the checks produce false positives.

Examples from the www monorepo (125 "unused" deps, almost all false positives):
- Package `cloudflare` installs module `cf` → "cli declares dependency on cloudflare but no source file imports it"
- Package `core` installs module `www_core` → "cli declares dependency on core but no source file imports it"
- Package `namecheap` installs module `www_namecheap` → same pattern

This also causes false positives in `dead-workspace-packages` ("library 'core' is not imported by any workspace package") and `dead-modules`.

## Root cause

The checks resolve package names to import names using a heuristic (replace hyphens with underscores). But many packages use custom import names configured via `[tool.hatch.build.targets.wheel] packages = ["cf"]` or `src/` layout with a different directory name.

## Proposed fix

Read the actual import name from each package's build config:
1. Check `[tool.hatch.build.targets.wheel] packages` for explicit package dirs
2. Check `src/` layout directories
3. Fall back to the hyphen-to-underscore heuristic only when no build config exists

Alternatively, support an `import_name` field in workspace.toml project entries for explicit mapping.

## Also: ruff line-counting bug

`rlsbl check --name ruff-lint` reports the count of output LINES from `ruff check --quiet`, not the count of errors. Ruff's quiet output includes multi-line context for each error. The www monorepo had 255 actual errors but rlsbl reported 2648. After fixing all 255, the check may still report a nonzero count.

## Affected files

- `rlsbl/checks/quality.py` — deps-undeclared, deps-unused, dead-workspace-packages checks
- `rlsbl/lint/__init__.py` — ruff output parsing
