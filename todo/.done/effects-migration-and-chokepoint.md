# Effects migration + internal effect chokepoint

Supersedes `globals-redesign-migration.md` (moved to `.obsolete/`; its danger list, command
count, and watch item were stale). Provenance: `[%%]`-marked decisions were adopted from
recommendations (freely reversible); unmarked were deliberate user rulings.

## Part A — internal effect chokepoint (independent; can start before strictcli ships)

Converge every effectful call through one module so the later `ctx.effects` migration is a
one-file adaptation. Current census (verified 2026-07-28):

- ~118 direct `subprocess.run/Popen/check_output/check_call` sites across 51 modules bypass
  `utils.run` (heaviest: `commands/init_cmd.py` 12, `testing.py` 7, `releasable_migration.py` 6,
  `commands/watch.py` 6, `commands/monorepo/extract.py` 6, `prepush_utils.py`, `git_util.py`,
  `commands/release/{validate,execute}.py`, `commands/push_cmd.py`, `commands/claim_name.py`).
- ~84 direct filesystem mutations (monorepo 32, changelog 24, release 17, init_cmd 11).
- ~21 mutating network/gh verbs (release create/delete/edit, workflow run, npm/cargo/uv
  publish-family) + 39 `run_gh` sites.

Work: widen `utils.run`/`run_gh` into the single effects module (subprocess incl. streaming
Popen, fs write/rename/chmod, network mutations); behavior-neutral; then a permanent backstop
test asserting no production module bypasses it (explicit tiny exemption list). Suite green at
every step.

Coordination: `commands/init_cmd.py`, `checks/project.py`, `data/checks.toml` are hot files
shared with the in-flight state-layer program — coordinate edits.

## Part B — migration onto the strictcli effects regime (needs the strictcli release)

1. Delete the three app-level globals (`__init__.py:159-162`); opt into the framework reserved
   flags; classify all **51** commands `read_only`/`mutating`.
2. The **danger 9** (verified current list; the old todo's "danger 12" is stale — `record-gif`
   deleted, `monorepo add` + `monorepo mirror` now honor dry-run): `commit`, `release init`,
   `monorepo init`, `monorepo remove`, `monorepo sync`, `monorepo snapshot`,
   `monorepo release init`, `dev install`, `dev sync`. Ruling `[%%]`: all get
   `dry_run_supported=false` + reason, EXCEPT `monorepo sync` and `dev sync` (and `dev install`
   if trivially close) which get real recorded-effects previews.
3. **Watch: delete** `--watch-async`, detached watchers, pidfiles, `watch --stop` per ledger
   item 8.2 (ledger provenance is itself `[%%]`). Scope: `commands/watch.py` ~lines 475-730;
   flags at `__init__.py:912-913`; the four MutexGroups (`__init__.py:246-249, 384-387,
   497-500, 1358-1361` collapse to plain `--watch`/`--no-watch`); spawn sites
   (`release/execute.py:2166`, `monorepo/batch_release.py:421,561`, `release_retry.py:319`);
   `release/shared.py` `build_release_flags`; 8 dedicated + 6 incidental test files; docs/help.
4. Snapshot split: the clean seam already exists in `monorepo/snapshot_cmd.py` — mutating
   write-half gains real dry-run; the `--check` half becomes a `read_only` command.
5. Self-spawns route through `effects.spawn` (token inheritance): `monorepo/commands.py:202,217`,
   `init_cmd.py:1531`, `mirror_cmd.py:484`, `changelog_cmd.py:1148`.
6. Handshakes: declare `RLSBL_RELEASE_PUSH`, `RLSBL_SCRUB_ORCHESTRATED`, `RLSBL_PUSH_STDIN` via
   strictcli's handshake-env primitive; the in-progress corroboration in `git_util.py:243-261`
   stays as policy code (note: the ledger's revert-or-forward engine will change its source).
7. Delete the interim prompt stopgaps in `claim_name.py`, `push_cmd.py`, `deprecate.py`,
   `undo.py` (superseded by framework `--yes`).

Release: Parts A+B ship in this repo's own release after completion. The in-flight state-layer
strand releases independently whenever its owners judge it coherent (deliberate ruling —
decoupled release streams).
