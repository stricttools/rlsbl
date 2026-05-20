# Workspace health dashboard (unified check system)

## Context

Workspace health checks are scattered across multiple commands: `monorepo lint` (2 structural checks), `rlsbl doctor` (11 per-project checks + 3 ad-hoc monorepo checks), `changelog validate` (5 checks), `monorepo outdated` (constraint evaluation). There's no single command that surfaces all workspace problems.

## Decisions

- **Unify all checks under the strictcli check system.** All existing checks migrate to `@check`-decorated functions. `rlsbl check --all` replaces `doctor`. `rlsbl check deps.*` replaces `monorepo outdated`. `rlsbl check structure.*` replaces `monorepo lint`.
- **All checks are errors** (nonzero exit on failure).
- **Check system is a strictcli feature**, not rlsbl-specific. See `strictcli/todo/check-system.md`.

## Migration plan

### Existing checks to migrate

From `doctor.py` (11 registered + 3 ad-hoc):
- `lock` -> `check project.lock`
- `versions` -> `check project.version-consistency`
- `names` -> `check project.name-consistency`
- `license` -> `check project.license`
- `description` -> `check project.description`
- `local-tag` -> `check release.local-tag`
- `remote-tag` -> `check release.remote-tag` (needs_network)
- `github-release` -> `check release.github-release` (needs_network)
- `branch-sync` -> `check release.branch-sync` (needs_network)
- `changelog` -> `check changelog.entry`
- `library-lint` -> `check lint.library-boundaries`
- `_check_router_exists` -> `check workspace.ci-router`
- `_check_workflows_synced` -> `check workspace.ci-synced`
- `_check_project_targets` -> `check workspace.targets`

The 3 ad-hoc monorepo checks need their signatures normalized (currently take `(root, projects)` args; the check system provides these via `CheckContext`).

From `monorepo lint`:
- Unregistered projects -> `check workspace.unregistered`
- Stale entries -> `check workspace.stale-entries`

From `changelog validate`:
- Hash resolution -> `check changelog.hashes`
- Tag-based range -> `check changelog.range`
- Commit coverage -> `check changelog.coverage`
- Orphan detection -> `check changelog.orphans`
- Schema conformance -> `check changelog.schema`

### New checks from other todos

As features are implemented:
- `check deps.stale` -- stale internal deps (from coordinated-release)
- `check deps.unused` -- unused declared deps (from dependency-import-validation)
- `check deps.undeclared` -- undeclared imports (from dependency-import-validation)
- `check layers.violations` -- architectural layer violations (from architectural-layer-rules)
- `check workspace.cycles` -- dependency cycles (from workspace graph, currently detected by `topological_order()`)

### Check groups (in config)

```toml
[check.groups]
quick = ["project.*", "workspace.unregistered", "workspace.stale-entries", "changelog.schema"]
full = ["*"]
pre-push = ["changelog.coverage", "workspace.*"]
deps = ["deps.*"]
release = ["project.*", "changelog.*", "release.*"]
```

## Backward compatibility

- `rlsbl doctor` becomes an alias for `rlsbl check --all` (or a check group).
- `monorepo lint` becomes an alias for `rlsbl check workspace.unregistered workspace.stale-entries`.
- `rlsbl changelog validate` becomes an alias for `rlsbl check changelog.*`.
- Old commands can emit deprecation warnings pointing to the new check commands.

## Prerequisites

- Unified check system in strictcli (see `strictcli/todo/check-system.md`)
- Each feature todo that adds checks (layer rules, import validation, etc.)

## Effort

Medium. Most of the check logic already exists -- the work is migration (adapting signatures, registering with the new system, defining groups) not new feature development.
