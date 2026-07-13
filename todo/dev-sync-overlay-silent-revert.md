# `rlsbl dev sync` overlay is silently reverted by any bare `uv run`

## Context

`rlsbl dev sync` overlays local editable checkouts of sibling projects onto a
project's locked environment. It reads `dev-sources.toml.local-only`, runs
`uv sync --inexact` with a `--no-install-package <pkg>` exclusion per overlay,
then `uv pip install -e <path>` for each entry. The command already recognizes
that bare `uv run` auto-syncs and would wipe the overlays, so it hard-gates at
entry: `run_sync` refuses to proceed unless `UV_NO_SYNC=1` is present in the
environment, and prints a detailed error otherwise.

## Problem

The `UV_NO_SYNC=1` requirement is enforced **only at sync time**. It does not
persist. After a successful `dev sync`:

- Any later `uv run` in that project *without* `UV_NO_SYNC=1` in the
  environment quietly re-syncs the venv from the registry lockfile and wipes the
  editable overlays.
- Imports of unreleased sibling APIs then fail at runtime -- or, worse, silently
  resolve to the stale *released* code from the registry wheel, so tests pass
  against the wrong code with no error.

This is exactly the silent-degradation failure class that rlsbl exists to
prevent: the same command (`uv run ...`) produces different behavior depending
on whether an env var happens to be set in the current shell, and the
degradation (overlay wiped, stale code substituted) is completely silent. The
current mitigations are:

- the entry-time hard gate (only protects the `dev sync` invocation itself), and
- a printed reminder at the end of a successful sync ("a bare `uv sync` reverts
  overlays; re-run `rlsbl dev sync` to restore them") -- guidance an agent will
  ignore.

There is currently no post-sync mechanism that *detects* a wiped overlay and
fails hard, and nothing that makes `uv` itself refuse to auto-sync
persistently. (A related but distinct guard exists in the release path -- the
overlay version-skew abort in `release/validate.py` -- but that fires during
release, not during ordinary development, and does not detect an already-wiped
overlay.)

## Solutions

### Option A -- Sentinel + `rlsbl dev status`/`check` drift detection + hard-failing hook

`dev sync` records a sentinel describing the intended overlay state (e.g. a
project-local marker file, or a fingerprint recorded under `.rlsbl/`), capturing
per-package the expected editable path and version. Add a `rlsbl dev status`
(or `rlsbl dev check`) command and/or a registered check that compares the
sentinel against the *actual* installed environment (via `uv pip show <pkg>` /
inspecting the `.dist-info` `direct_url.json` for an editable install) and fails
hard when a declared overlay is no longer editable (i.e. was wiped back to the
registry wheel). Wire this as an `external_check` and/or a pre-test check so the
test suite refuses to run against a silently-reverted environment.

Pros:
- Turns the silent degradation into a loud, deterministic hard error at the
  moment it matters (before tests run).
- Fits the existing check framework (`@app.check(...)` returning `CheckResult`,
  registered in `CHECK_TARGETS`) and the `external_checks` mechanism, so it
  composes with `rlsbl check` and the release preflight.
- No dependence on the developer's shell env for correctness -- the check is the
  guardrail, not a reminder.

Cons:
- Detection is *reactive*: it catches the wipe after the fact rather than
  preventing it. Still requires the developer/agent to run the check (mitigated
  by wiring it into the pre-test/external-check path).
- Adds a sentinel file to manage (creation, staleness, and making sure it is
  git-invisible like `dev-sources.toml.local-only`).

### Option B -- Make `uv` itself refuse to auto-sync persistently (preventive)

Investigate uv configuration that persists the "do not auto-sync" intent
*per project* without committing anything, so a bare `uv run` cannot revert the
overlay in the first place. Candidates to evaluate against the pinned uv version:

- a project-local `uv.toml` / `[tool.uv]` setting that disables automatic
  environment syncing on `uv run` (equivalent to `--no-sync` / `UV_NO_SYNC`),
  if uv exposes one that is honored without an env var;
- writing the setting into a git-invisible local config (matching the
  `*.local-only` gitignore convention) so it is never committed;
- `dev sync` emitting/refreshing that local config as part of its run.

Pros:
- Preventive rather than reactive: the wipe never happens, so there is no window
  where stale code can silently substitute.
- Removes reliance on every shell exporting `UV_NO_SYNC=1`; correctness no
  longer depends on shell state.

Cons:
- Depends entirely on uv exposing a persistable, file-scoped opt-out that is
  honored for `uv run` without an env var -- must be verified against the pinned
  uv version; uv may only honor `UV_NO_SYNC` via env/flag, in which case this
  option is not viable as stated.
- A committed config setting would leak machine-local dev intent into the repo;
  a git-invisible one must be reconciled with uv's config discovery rules
  (whether uv reads a non-standard local file).
- Interacts with the existing entry-time gate -- if uv is configured to never
  auto-sync, the `UV_NO_SYNC=1` env gate may become redundant and should be
  reconsidered rather than left as a second, divergent guard.

### Option C -- Documentation only (weakest)

Strengthen the printed reminder and docs (CLAUDE.md / docs) to spell out that
`UV_NO_SYNC=1` must be exported for the whole session (shell profile or
`.envrc`), and that any `uv run` without it wipes overlays.

Pros:
- Trivial; no code change.

Cons:
- Relies on humans/agents reading and obeying guidance -- precisely the soft
  guardrail this codebase's philosophy rejects ("hard errors, not warnings";
  agents ignore every warning). Does nothing to detect or prevent the silent
  wipe. Weakest option; insufficient on its own.

### Recommendation

Prefer **Option B if uv exposes a persistable per-project opt-out** (prevention
beats detection and eliminates the silent-substitution window entirely); the
first task is a spike to confirm what the pinned uv version actually honors.
If B is not viable, implement **Option A** so a wiped overlay becomes a hard
failure before tests run. Option C is documentation that should accompany
whichever mechanism lands, never the sole fix.

## Affected files

- `rlsbl/commands/dev_sync.py` -- `run_sync` (the `UV_NO_SYNC` entry gate at the
  top; the `uv sync --inexact --no-install-package ...` invocation; the
  per-entry `uv pip install -e`; the closing "bare `uv sync` reverts overlays"
  reminder). This is where a sentinel would be written (Option A) or a local uv
  config emitted (Option B). Constants `OVERRIDES_FILENAME` and
  `_UV_NO_SYNC_ERROR` live here too.
- `rlsbl/__init__.py` -- `cmd_dev_sync` (the `dev sync` dispatch, around the
  `@dev.command(name="sync")` registration). A new `rlsbl dev status`/`check`
  subcommand (Option A) would be registered alongside it under the `dev` group.
- `rlsbl/checks/__init__.py` -- `CHECK_TARGETS` (register a new
  overlay-drift check name and its target/tag) if Option A adds a framework
  check.
- `rlsbl/checks/project.py` -- add the `@app.check(...)` implementation that
  compares the sentinel against the installed environment and returns a failing
  `CheckResult` when an overlay was wiped (Option A). The existing
  `cross-repo-path-sources` check here is the closest sibling pattern and also
  references `dev-sources.toml.local-only`.
- `rlsbl/external_checks.py` -- wiring the drift check as an external/pre-test
  check so it hard-fails before the test suite runs (Option A).
- `rlsbl/commands/release/validate.py` -- the existing dev-sources overlay
  version-skew abort (`_abort_on_version_skew` and the surrounding overlay
  reader that imports `_load_overlays` / `OVERRIDES_FILENAME`); keep the new
  detection consistent with this release-time guard and avoid divergent logic.
- Tests: add regression coverage that simulates an overlay being wiped by a bare
  sync and asserts the new check/status fails hard (Option A), or that the
  persisted uv config prevents the auto-sync (Option B). Place alongside existing
  dev-sync/overlay tests in the suite.

## Effort estimate

- Option A: medium -- ~0.5-1 day (sentinel format, status/check command, a
  framework check, external-check wiring, red-green tests).
- Option B: small-to-medium, front-loaded by a spike -- a few hours to confirm
  uv's persistable opt-out, then a small change if one exists; larger if it
  requires generating/managing a git-invisible local config.
- Option C: trivial (documentation), but not a standalone fix.
