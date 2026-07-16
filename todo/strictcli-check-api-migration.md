# Migrate rlsbl to strictcli's new check API (CLI currently crashes at import)

## Context

strictcli replaced its check system between 0.24.2 and 0.27.0-dev:

- `CheckResult(status, message, details=)` — **removed**. `CheckRunResult(name, outcome)` exists but is a different concept (the runner's named result), not a constructor-compatible replacement.
- `App.check(name)` decorator (impl `(ctx) -> CheckResult`) — **removed**. Replaced by `App.error_check(name)` / `App.warn_check(name)`, impl signature `(ctx, reporter) -> _CheckOutcome`, with per-check severity declared in checks.toml.
- New reporter model: `ErrorReporter`/`WarnReporter` (`passed`/`skipped`/`found`, `.error()` only on ErrorReporter, `.warn()` on both), plus `SkipCheck`.
- Survived unchanged: `App.set_scope_adapter`, `App.register_check_provider`, `CheckContext`.

## Problem

Every `rlsbl` CLI invocation crashes at import: `rlsbl/checks/scope.py:17` does `from strictcli import CheckResult` (ImportError on current strictcli). Fixing only the import moves the crash to `register_project_checks()` calling the removed `@app.check` (AttributeError).

The test suite does NOT reproduce this: rlsbl's `uv.lock`/`.venv` resolves strictcli 0.24.2 (old API), while the globally-installed editable rlsbl imports editable strictcli HEAD. `uv run pytest` passes vacuously against the old API.

## Scope of the migration

- 9 source files import `CheckResult`: `rlsbl/checks/{scope,project,release,changelog,workspace,quality,prepush,__init__}.py`, `rlsbl/external_checks.py`.
- 6 files register ~61 checks via `@app.check` returning `CheckResult` — each impl must move to `@app.error_check`/`@app.warn_check` per its declared severity in `rlsbl/data/checks.toml` (already in the new severity-per-check format), take `(ctx, reporter)`, and map pass/skip/warn/fail + `details=` onto reporter calls.
- ~13 test files import `CheckResult` and must be migrated.
- `uv.lock` must be advanced off 0.24.2 so the suite actually runs against the new API (and guards against regression).

## Design decisions required (blockers for a mechanical migration)

Severity is now bound at the decorator; a `warn_check` cannot emit a fail. Impl-vs-declaration mismatches must be audited per check:

- Concrete conflict found: `github-release` is declared `severity="warn"` in checks.toml but its impl returns `CheckResult("fail", ...)` (gh missing/unauthenticated).
- Files with mixed warn+fail returns needing the same audit: `changelog.py` (7 fail / 5 warn), `project.py` (9 fail / 8 warn), `release.py` (4 fail / 6 warn), `quality.py` (3 fail / 1 warn).

Per check, decide: promote the declared severity to error, or demote the fail branch to warn. The ecosystem's "hard errors, not warnings" philosophy suggests promotion as the default posture, but each check should be judged on whether its failure must block an operation.

## Effort

L. Mostly mechanical once the per-check severity audit is decided; the audit itself is the real work (~61 checks). Red-green: advance `uv.lock` first so the import crash reproduces in the suite, then migrate until green.
