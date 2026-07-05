# Monorepo consumer needs: root selfdoc, pnpm workspace CI, publish.json docs drift

## Context

A consumer monorepo (Go + npm workspace members, one releasable group, root-level docs site) surfaced three gaps (a fourth, multi-strictcli-mains, was split to .defer/) during setup planning. All four were verified against rlsbl source before filing.

## 1. Repo-level selfdoc.json is invisible to the release pipeline

**Problem:** `_run_selfdoc_gen` / `_run_selfdoc_check` (rlsbl/commands/release/validate.py) look for `selfdoc.json` in the member project directory only. A monorepo with a single root-level docs site (one `selfdoc.json` at the workspace root, covering all members) gets no selfdoc steps during release — generated root files (README/CLAUDE, chmod 444) and the docs site rot silently, which is exactly the staleness class the selfdoc integration exists to prevent.

**Workaround (works today):** releasable-level hook in `releasables/<name>/config.json`:
`{"cmd": "selfdoc gen --no-auto-commit && selfdoc check --no-auto-commit", "dir": "."}`

**Solutions:**
- (a) **Recommended:** during `monorepo release run`, detect `selfdoc.json` at the workspace root and run gen/check once (before per-member steps). Keeps parity with the standalone flow.
- (b) Document the hook recipe as the blessed pattern. Zero code, but the guarantee stays tribal knowledge and every consumer rediscovers it.

**Affected:** rlsbl/commands/release/validate.py, monorepo release flow, docs/monorepo.md.
**Effort:** S–M.

## 2. pnpm workspace lockfile silently not frozen in synced CI

**Problem:** templates/npm/ci-pnpm.yml.tpl does `if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; else pnpm install; fi`. After `monorepo sync` injects `working-directory: <project>/`, the workspace-root lockfile is not seen, so CI silently runs a non-frozen install. The lockfile guarantee everyone assumes CI enforces does not exist. This is a silent-degradation bug by house philosophy — same input (lockfile committed at root), different behavior.

**Solutions:**
- (a) **Recommended:** workspace-aware template: when the lockfile exists at the git root but not the project dir, install from the workspace root with `pnpm install --frozen-lockfile --filter <project>`.
- (b) Hard-error when no lockfile is found anywhere (forces the consumer to notice), combined with (a).

**Affected:** templates/npm/ci-pnpm.yml.tpl, sync path-injection logic, tests.
**Effort:** S. Red-green: a workspace fixture whose synced CI must contain `--frozen-lockfile`.

## 3. `publish.json` docs drift

**Problem:** rlsbl docs and the shared ~/Projects/CLAUDE.md rlsbl section describe `.rlsbl-monorepo/releasables/<name>/publish.json` (releasable-level publish config, per-package overrides). A source audit found zero references to `publish.json` in code — releasable state appears to be `version`, `changes/`, `config.json`, `CHANGELOG.md` only, with publish/private expressed in member config. Either the feature was removed/renamed and docs drifted, or the audit missed it. Determine which; fix the authoritative side (docs or code), and update the shared CLAUDE.md section to match.

**Affected:** docs/monorepo.md, ~/Projects/CLAUDE.md rlsbl section, possibly releasable scaffolding.
**Effort:** S.
