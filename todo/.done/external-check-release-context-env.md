# Inject release-context env vars into external-check subprocesses

## Context

External checks (`rlsbl/external_checks.py`) run consumer-declared gate
commands during `rlsbl check` and release preflight. Today they execute in
near-total context blindness:

- `_make_external_check_fn` (~line 381) and `_make_structured_check_fn`
  (~line 414) call `subprocess.run(...)` with **no `env=` argument** — the
  subprocess inherits the ambient environment and receives nothing from
  rlsbl: no tag, no range, no version, no scope.
- Meanwhile hooks DO get rich context: `build_hook_env`
  (`rlsbl/commands/release/hooks.py` ~line 392) injects `RLSBL_VERSION`,
  `RLSBL_BUMP_TYPE`, `RLSBL_PREV_VERSION`, `RLSBL_DESCRIPTION`,
  `RLSBL_PACKAGE` — but only for hook subprocesses, never for checks.
- The primitives an external check most often needs already exist and are
  monorepo-aware: `get_last_version_tag(tag_glob)` (`rlsbl/utils.py` ~line
  157, handles shallow clones and first releases) and `_unreleased_range`
  (`rlsbl/changelog/resolve.py` ~line 80), already reused across modules
  (e.g. `commands/status.py`).

## Problem

A growing class of external checks are release-diff shaped: "compare a
committed artifact against its state at the last release tag" (e.g.
additivity/compatibility gates over generated schema or contract files), or
more generally "operate on the unreleased range." Today every such check must
rediscover git state itself with its own `git describe` — duplicating logic
rlsbl already owns, and getting it WRONG in monorepos unless the check
reimplements rlsbl's per-project tag-glob scheme (`<name>@v*` vs `v*`). The
tool knows the correct answer and withholds it.

## Proposed solutions

### A. Inject a small, guaranteed context set; version pair only when computable (recommended)

Merge into the subprocess env (over `os.environ`) for both freeform and
structured checks:

- `RLSBL_LAST_TAG` — from `get_last_version_tag()` with the project's tag
  glob; empty string when no tag exists (first release), so checks can
  distinguish "no baseline" explicitly.
- `RLSBL_UNRELEASED_RANGE` — from `_unreleased_range()` (`<tag>..HEAD` or
  `HEAD`).
- `RLSBL_PROJECT_ROOT` — the resolved project root (external checks with a
  `cwd` override currently have no way to know it).
- `RLSBL_VERSION` / `RLSBL_PREV_VERSION` — only set when running inside a
  release flow where they are computed (preflight); absent during plain
  `rlsbl check`. Document the availability matrix explicitly — a check must
  not assume them.

Pros: ~10-30 lines in `external_checks.py`; backward-safe (new env keys
only); monorepo-correct by construction; makes release-diff checks trivial
(`git show "$RLSBL_LAST_TAG:path"`). Cons: env availability differs between
`check` and release preflight for the version pair — hence the explicit
matrix in docs.

### B. Full hook-env parity for checks

Reuse `build_hook_env` wholesale for check subprocesses. Pros: one env
mechanism. Cons: most hook vars (`RLSBL_DESCRIPTION`, `RLSBL_BUMP_TYPE`) do
not exist at check time at all, forcing either dummy values (bad) or a
mostly-empty contract; hook env also carries release-only semantics that
would mislead check authors. Rejected as the primary shape, though the
implementation should share a helper with `build_hook_env` rather than
duplicating the merge logic.

### C. Opt-in per check (`env_context = true` in the external_checks entry)

Pros: zero change for existing checks. Cons: needless knob — env injection
is invisible to checks that ignore it; opt-in adds config surface for no
protection. Rejected.

## Notes for implementation

- Cache the tag/range computation once per check run, not per external check
  (N checks must not mean N `git describe` calls).
- Monorepo: compute per-releasable/per-project tag glob exactly as the
  changelog layer does; in workspace contexts the value must be scoped to the
  sub-project the check belongs to.
- Tests: extend `tests/test_external_checks.py` with an env-asserting fixture
  (a check command that fails unless `RLSBL_LAST_TAG`/`RLSBL_UNRELEASED_RANGE`
  are present and correct, in both tagged and untagged repos, and in a
  monorepo fixture). Docs: `docs/configuration.md` external-checks section
  gains the env table with the availability matrix.
- While in the area: the external-check docs' surrounding ecosystem guidance
  still shows a config example without the now-mandatory `kind` field
  (`validate_external_checks` hard-errors on it, ~line 124). Sweep docs for
  stale examples as part of this change.

## Affected files

- `rlsbl/external_checks.py` (both `_make_*_check_fn`, plus a small shared
  env-builder helper)
- `rlsbl/commands/release/hooks.py` (extract/share the env-merge helper)
- `docs/configuration.md`, `docs/checks.md`
- `tests/test_external_checks.py`

## Effort

Small. Core change is tens of lines plus tests and docs; the only design
care is the availability matrix and monorepo tag-glob scoping.
