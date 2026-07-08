# Monorepo checks misbehave with virtual workspace roots and releasables

## Context

Converting a consumer monorepo to the releasable model (explicit `[[releasables]]` in
`workspace.toml`, with a virtual uv workspace root — a root `pyproject.toml` that has
`[tool.uv.workspace]` but no `[project]` section) surfaced five distinct bugs in the
monorepo check suite. The conversion itself succeeded; these are check-layer defects
that produce false reds (or misleading output) in any monorepo shaped like this.

## Bugs

### 1. `check_changelog_orphans` ignores tag_glob/project scoping

`check_changelog_orphans` calls `check_no_orphans` without passing `tag_glob` /
`project` the way the range and coverage checks do. Result: orphan detection uses the
default `v*` glob for EVERY releasable, so in a monorepo with per-releasable tag
formats (e.g., `auth-v*`, `www-v*`) the unreleased range is computed against the wrong
last tag and entries get falsely flagged (or real orphans are missed).

**Proposed fix:** thread the same `tag_glob`/`project` parameters into
`check_no_orphans` that `check_changelog_range` and `check_changelog_coverage` already
receive. Audit all call sites of `check_no_orphans` for the same omission.

### 2. `workspace-ci-synced` expects root `{name}-ci.yml` files that inline sync never writes

The `workspace-ci-synced` check verifies that root-level `.github/workflows/{name}-ci.yml`
files exist for each sub-project. But the inline CI sync path (router + inlined jobs)
writes no such per-project root workflow files. Consequence: the check is red in every
freshly-synced monorepo, immediately after running the tool's own sync command.

**Proposed fix:** make the check aware of the sync mode. If the workspace uses the
inline router, the check should verify the router workflow contains the expected
per-project jobs (or simply that the router file is up to date), not demand
`{name}-ci.yml` files. The check and the sync writer must share one source of truth for
what files are expected.

### 3. `releasable-residue` contradicts `library-lint`

`releasable-residue` demands that member-level `.rlsbl/lint/` directories be deleted
(treating them as residue after migrating to a releasable). But `library-lint` reads
lint configuration ONLY from the member level. Following one check's demand makes the
other check lose its config — the two checks are contradictory for library members of a
releasable.

**Proposed fix:** decide where lint config lives in the releasable model (releasable
level with per-member overrides seems most consistent with the config.json override
pattern), then update whichever check disagrees. Until both read from the same place,
exempt `.rlsbl/lint/` from the residue list.

### 4. Root-context checks treat a virtual uv workspace root as a broken project

`config-schema`, `private-publish-workflow`, `target-version-readable`, and
`name-consistency` all run against the workspace root and assume it is a real project.
A virtual uv workspace root (`pyproject.toml` with `[tool.uv.workspace]` but no
`[project]` table) has no name, no version, and no publish target — these checks then
error as if the root were a misconfigured package.

**Proposed fix:** detect the virtual-root shape (pyproject present, `[project]` absent,
`[tool.uv.workspace]` present) once, centrally, and have root-context checks either
skip with an explicit "virtual workspace root" result or validate only the fields that
exist. Do not silently pass — report SKIP with the reason.

### 5. Cosmetic: `monorepo status` shows Tag "(none)" despite a matching tag

`rlsbl monorepo status` displays Tag "(none)" even when a tag matching the configured
`tag_format` exists in the repo. Likely the status command uses the default `v*` glob
(same root cause family as bug 1) instead of the releasable's tag_format when resolving
the last tag.

**Proposed fix:** use the releasable's `tag_format`-derived glob in the status
command's tag lookup. Cosmetic but misleading during release triage.

## Affected files (where known)

- Orphan check plumbing: the changelog checks module (wherever
  `check_changelog_orphans` / `check_no_orphans` / `check_changelog_range` live)
- `workspace-ci-synced` check + the inline CI sync writer
- `releasable-residue` check + `library-lint` config loader
- Root-context checks: `config-schema`, `private-publish-workflow`,
  `target-version-readable`, `name-consistency`
- `monorepo status` command implementation

## Effort

- Bug 1: small (parameter threading + regression test with a non-default tag glob)
- Bug 2: medium (needs a mode-aware definition of "synced"; shared expectation model)
- Bug 3: medium (design decision on lint config location, then mechanical)
- Bug 4: medium (central virtual-root detection + per-check SKIP handling)
- Bug 5: small (glob fix + test)

Each fix needs a red-green regression test reproducing the false red against a fixture
monorepo with a virtual workspace root and non-default tag_format.
