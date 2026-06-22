# Monorepo scaffolding overhead

## Problem

In a monorepo with many packages sharing a releasable, rlsbl scaffolds and manages per-package files that are largely identical. The releasable model (0.78.0) solved the release ceremony problem (one release per group instead of one per package), but the scaffolding and config management layer still operates per-package.

This creates three concrete problems: blast radius on cross-cutting changes, maintenance of duplicate files, and disproportionate churn in the git history.

## Evidence

All examples are from the www monorepo (44 packages in the "www" releasable, 5 in "auth").

### Identical per-package config files

52 `.rlsbl/config.json` files exist. Diffing any pair returns nothing:

```
$ diff core/.rlsbl/config.json dns-protocol/.rlsbl/config.json
$ diff core/.rlsbl/config.json hetzner/.rlsbl/config.json
```

Every file contains:
```json
{
  "private": true,
  "target": "pypi",
  "push_timeout": 120,
  "targets": ["pypi"],
  "pipelines": {}
}
```

No package overrides any field. The only config that differs is at the monorepo root (which adds a `pipelines.assets` block).

### Blast radius of a single config field change

When rlsbl 0.45.0 made `push_timeout` mandatory, a single commit (`945901d`) touched 51 config.json files:

```
 .rlsbl/config.json                 | 3 ++-
 alerting/.rlsbl/config.json        | 2 +-
 analytics/.rlsbl/config.json       | 2 +-
 auth-gateway/.rlsbl/config.json    | 3 ++-
 ...
 vultr/.rlsbl/config.json           | 2 +-
 51 files changed, 67 insertions(+), 51 deletions(-)
```

Every future mandatory config field will produce the same pattern.

### Identical hook scripts

52 packages x 3 hooks = 156 bash scripts. All are identical templates:

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "Post-release: v$RLSBL_VERSION"
```

Each has a corresponding merge base file in `.rlsbl/bases/` (525 base files total) for three-way merge tracking. None of the 52 packages in www has customized any hook.

### Scaffold churn

23 scaffold-related commits in www since 2026-05-01. Six are bare `rlsbl scaffold` commits that touch only rlsbl-managed files. Each re-scaffold regenerates CI workflows, lint configs, and base files across all packages.

### Per-package CI workflows

52 per-package `.github/workflows/{name}-ci.yml` files are generated from per-package source templates. Most are structurally identical (same matrix, same steps) differing only in working-directory and import-check module name.

### Residual per-package release state

50 per-package `.rlsbl/changes/` directories still exist from the pre-releasable era. These are now orphaned -- changelogs live at the releasable level -- but rlsbl scaffold doesn't know to clean them up.

## Potential solutions

### 1. Releasable-level config inheritance

Per-package config.json files that are identical to the releasable default would not need to exist. A releasable could define default config, and per-package files would only contain overrides:

```
.rlsbl-monorepo/releasables/www/config.json   # defaults for all 44 packages
cloudflare/.rlsbl/config.json                   # only if cloudflare overrides something
```

Packages with no `.rlsbl/config.json` inherit everything from the releasable. Packages with one only declare differences. This eliminates the 51-file blast radius for new mandatory fields.

### 2. Releasable-level hooks

If no package in a releasable customizes hooks, hooks could live at the releasable level only:

```
.rlsbl-monorepo/releasables/www/hooks/pre-release.sh
```

Per-package hooks would only exist for packages that genuinely need different behavior. This would eliminate 156 identical scripts and 525 merge base files.

This is related to the existing todo `replace-bash-hooks-with-config.md` which proposes eliminating hook scripts entirely in favor of config-driven commands. Either approach solves the duplication; config-driven commands solve it more completely.

### 3. Bulk re-scaffold with releasable scope

`rlsbl scaffold` could accept a `--releasable <name>` flag (or a `rlsbl monorepo scaffold` command) that re-scaffolds all packages in a releasable in one operation. This doesn't reduce the file count but reduces the manual effort and ensures consistency.

### 4. CI workflow templates at releasable level

Instead of 52 per-package CI workflow source files, a releasable could define a CI template with per-package variables (working directory, language, import name). The CI router + per-package workflows would be generated from this single template. Packages needing custom CI (e.g., Go packages with different build steps) could override.

### 5. Scaffold-aware cleanup of pre-releasable artifacts

When a monorepo migrates to the releasable model, scaffold could offer to clean up per-package `.rlsbl/changes/`, `.rlsbl/releases/`, and per-package CHANGELOG.md files that are now superseded by releasable-level equivalents. Currently this cleanup is manual (the www evolution plan dedicates an entire phase to it).

## Relationship to existing todos

- `replace-bash-hooks-with-config.md` — solves the hook duplication subset of this problem
- `unified-toml-config.md` — changing the config format is orthogonal but could be combined with adding inheritance
- `releasable-model-gaps.md` — the missing migration CLI would handle cleanup of per-package release state

## Scope

This is not about the package split itself (which enforces real dependency boundaries) but about rlsbl's per-package scaffolding model not accounting for the fact that packages in a releasable share most configuration. The releasable model already acknowledges this for versioning and changelogs -- extending the same principle to config, hooks, CI, and scaffold cleanup is the natural next step.
