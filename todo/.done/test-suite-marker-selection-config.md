# test-suite check: no way to exclude slow/integration tests from release gating

## Context / problem

The built-in `test-suite` check runs the project's ENTIRE pytest suite with a
single time budget (`check_timeout`). The pytest invocation is hardcoded:
`_resolve_pytest_invocation` (`rlsbl/testing.py:88`) builds
`["uv", "run", "pytest"]` (or a `--group`/`--extra` variant) and
`run_project_tests` → `_run_pypi_tests` (`rlsbl/testing.py:122`, `:171`) runs it
verbatim. The `config` dict is threaded in, but only `uv_sync_verbose` and
`check_timeout` are read from it — there is no way to append pytest args or a
marker expression.

Some projects legitimately have slow, credentialed, or costly integration tests
(marked e.g. `-m integration`) that require live external services or API
credentials. These tests should NOT gate every release: they can add minutes of
runtime and real monetary/token cost to a pipeline that runs on every version
bump. Today there is no supported way to run `pytest -m "not integration"` in the
release preflight (or in `rlsbl check`) while keeping test gating for the fast
suite.

The only levers that exist are all wrong for this:

- `check_timeout` / `RLSBL_CHECK_TIMEOUT` (`rlsbl/utils.py` `get_check_timeout`)
  only widens the time budget — the slow tests still run and still cost money.
- `external_checks` (config, run during `rlsbl check`/preflight) can only ADD
  checks alongside the built-in `test-suite`; there is no key to DISABLE the
  built-in test step, so declaring an external `pytest -m "not integration"`
  check just runs BOTH the filtered suite and the full suite.
- The pre-release hook override (`is_hook_customized`,
  `rlsbl/commands/release/hooks.py:277`; consumed at
  `rlsbl/commands/release/__init__.py:824`) DOES let a project take over
  testing: when `hooks.pre_release` is a non-empty list, the built-in
  `test-suite` + lint checks are skipped and the hook runs instead. But this is
  a blunt, all-or-nothing instrument: it also disables built-in lint and (per a
  known bug) config-declared `external_checks`, and it applies ONLY to the
  release preflight — the standalone `rlsbl check --all` / `--tag preflight`
  still runs the full suite with no way to exclude markers. It also discards the
  target detection and uv-invocation resolution the built-in check does for you.

So a project must currently choose between: (a) paying the integration-test cost
on every release, (b) inflating `check_timeout` and still paying that cost, or
(c) abandoning the built-in test/lint/external-check machinery entirely via a
custom hook. None of these is "run the fast suite as a gate, skip the slow one."

## Proposed solutions

### (a) A marker/args config key threaded through the pytest invocation — RECOMMENDED

Add an explicit config key (e.g. `test_markers` or `test_args`) in
`.rlsbl/config.json`, read by `run_project_tests` and appended to the command
built by `_resolve_pytest_invocation` for the pypi target. Example:

```json
{ "test_markers": "not integration" }
```

producing `uv run pytest -m "not integration"`, or the more general

```json
{ "test_args": ["-m", "not integration"] }
```

appended verbatim. This is file-driven (matches rlsbl's "file-driven over
flag-driven" philosophy), explicit (no env var, no implicit default — absent key
means run everything, exactly as today), and keeps the built-in check's target
detection, uv resolution, timeout, and result reporting. It fixes BOTH the
release preflight and standalone `rlsbl check`, since both go through the same
`test-suite` check.

- Pros: minimal, surgical; single code path; preserves all built-in machinery;
  works for `rlsbl check` and release preflight identically; honors "same input,
  same behavior"; explicit and discoverable via config docs/selfdoc.
- Cons: pypi-specific unless generalized (Go already uses `-short`; npm/maven
  would need their own conventions); a per-target shape may be wanted later.
- Design note: prefer `test_markers` (pytest-specific, self-documenting) over a
  raw `test_args` list if the scope is deliberately pytest-only; or support both
  with `test_args` as the general escape hatch. Do NOT add an env-var lever —
  keep it config-only per "no implicit defaults / mandatory-explicit".

### (b) A config key to designate the built-in test step as replaced by an external check

Add a key that lets a project opt the built-in `test-suite` check OUT while
declaring the replacement as an `external_checks` entry (so it still runs during
both `rlsbl check` and preflight, unlike the hook override). Example: a
`disable_builtin_checks: ["test-suite"]` list plus an `external_checks` entry
running `uv run pytest -m "not integration"`.

- Pros: general (works for any built-in check, any command); keeps everything
  inside the check framework (visible in `--list`, honored by `rlsbl check`).
- Cons: more surface area; two config stanzas to express one intent; requires
  the `external_checks` bugs (see `external-checks-never-run.md`,
  `external-checks-invisible-to-check-command.md`) to be fixed first, else the
  replacement silently never runs.

### (c) Per-target test config block

A structured `test` config section keyed by target
(`{"test": {"pypi": {"markers": "not integration"}, "go": {"tags": "..."}}}`)
threaded into each `_run_*_tests`.

- Pros: cleanest long-term model; extends naturally to Go build tags, npm test
  script selection, etc.
- Cons: largest change; likely overkill if only pytest markers are needed now.
  Could be the eventual home that (a) migrates into.

## Affected files

- `rlsbl/testing.py` — `_resolve_pytest_invocation` (`:88`), `run_project_tests`
  (`:122`), `_run_pypi_tests` (`:171`): read the new key from `config` and
  append to the pytest command.
- `rlsbl/checks/quality.py` — `check_test_suite` (`:301`) already passes
  `ctx.config` into `run_project_tests`; no change needed if the key is read
  inside `testing.py`.
- Config docs / selfdoc — document the new key (what it does, that absent = run
  everything, that it is a selection filter not a gate bypass).
- New regression test in `tests/test_testing.py` asserting the marker/args are
  appended to the invocation and that an absent key preserves today's behavior.

## Effort estimate

Small for solution (a): thread one config key through
`_resolve_pytest_invocation`/`_run_pypi_tests`, add a docs entry and a
red-green test. Medium for (b) or (c).
