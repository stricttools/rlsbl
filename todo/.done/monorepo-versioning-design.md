# Monorepo versioning design: identity, unversioned packages, root changelog

## Problem

The current monorepo model requires every package to be independently versioned, even when packages are internal-only and never published. In product monorepos like WWW (50 packages) and F (43 packages), this creates ceremony without value: all packages sit at 0.1.0, changelogs are empty, and versioning is purely scaffolding.

Three interconnected gaps:

1. **No monorepo-level identity.** The monorepo as a whole has no version. There's no way to say "WWW is at v2.0" — only individual package versions exist. For unified selfdoc docs (which aggregate all packages), there's no natural version to use.

2. **No way to opt out of versioning.** Internal utility packages that never publish to a registry still need versions, tags, and changelogs. A `versioned = false` flag in workspace.toml would let these packages exist in the dependency graph without participating in the release machinery.

3. **Root-level commits have no home.** With the new path-scoped changelog coverage, commits that only touch root files (workspace config, CI, docs) are auto-exempt from all packages. This is fine for housekeeping, but breaking cross-cutting changes could escape coverage entirely.

## Design space

Three models, not mutually exclusive:

- **Independent model** (current, fits strictcli): Packages are atoms with independent lifecycles. Monorepo is infrastructure. Root commits are auto-exempt.
- **Product model** (fits WWW, F): Monorepo has its own version. Packages are internal implementation. Root commits go in the monorepo changelog.
- **Hybrid**: Some packages independently versioned (they publish to registries), others unversioned or tracking the monorepo version.

## Proposed: opt-in via workspace.toml

Add a `[monorepo]` section to workspace.toml:

```toml
[monorepo]
version = "2.0.0"      # optional: monorepo-level version
versioned = true        # enables monorepo-level versioning

[[projects]]
path = "auth"
versioned = false       # opt out of per-package versioning
```

When `monorepo.versioned = true`:
- The monorepo itself gets a version, tag format (`v2.0.0`), and changelog
- Root-level commits go in the monorepo changelog
- selfdoc docs use the monorepo version
- `monorepo release` can bump the monorepo version

When `projects.versioned = false`:
- The package exists in the dependency graph and watch patterns
- It does NOT appear in `monorepo release`, `monorepo status`, or coverage checks
- It does NOT get tags or changelogs

## Observations from real monorepos

- **strictcli** (3 pkgs): Independent model. Python and Go have separate consumers. No unified version needed.
- **WWW** (50 pkgs): Product model. All private, all at 0.1.0, unified selfdoc docs, single CLI deployable.
- **F** (43 pkgs): Product model. All private, all at 0.1.0, unified selfdoc docs, Flutter app is only deployable.
- **gamehome** (5 pkgs): Product model. Private, Go services.

## Effort

Large. Touches workspace.toml parsing, release flow, changelog coverage, monorepo release, selfdoc integration, and version-consistency checks.
