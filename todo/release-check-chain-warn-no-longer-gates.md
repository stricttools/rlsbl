# Release check chain: WARN no longer gates downstream checks

## Context

The check framework (strictcli) is changing its `depends_on` semantics: a
dependency is now satisfied by PASS **or** WARN. Dependents are cascade-skipped
only when a dependency FAILED (or was itself cascade-skipped). Previously, a
WARN (without `--ignore-warnings`) also cascade-skipped dependents. Exit-code
behavior is unchanged: a WARN still makes the run exit non-zero unless
`--ignore-warnings`.

## Problem

The release check chain in `rlsbl/data/checks.toml` relies on the old
warn-cascade as a precondition gate:

- `local-tag` (warn severity) -> `remote-tag` -> `github-release`

`local-tag` returns WARN when the current version's tag does not exist locally
(the normal state mid-development, before a release). Under the old semantics
that WARN skipped `remote-tag` and `github-release`. Under the new semantics
both now RUN in that state:

1. `remote-tag` performs a network call (`git ls-remote --tags origin <tag>`)
   whose answer is near-certain (tag absent locally implies absent remotely in
   practice), then WARNs. Slower checks, needless network dependency, and an
   extra noisy WARN line.
2. `github-release` (in `rlsbl/checks/release.py`) hard-FAILs when the `gh`
   CLI is not installed or not authenticated -- **before** it ever looks at
   the tag. On a machine without `gh`, `rlsbl check --tag release` in a
   not-yet-tagged working tree now produces a FAIL where it previously
   produced a clean skip. That is a behavior regression: the not-yet-released
   state used to be non-failing.
3. If `gh` is present, `gh release view <tag>` is another network call that
   predictably WARNs.

## What rlsbl should do

Stop relying on warn-cascade for precondition gating; each downstream check
should self-gate with an explicit `skip` result:

- In `remote-tag`: before the `ls-remote`, check whether the tag exists
  locally (same probe `local-tag` uses). If not, return
  `CheckResult("skip", f"{tag} not created yet")` -- no network call.
- In `github-release`: same self-gate first (tag missing locally -> skip),
  BEFORE the `gh` installed/authenticated probes, so machines without `gh`
  are not failed in the not-yet-tagged state. Keep the gh FAILs for the case
  where the tag exists locally (then the check is genuinely actionable).
- Note that an explicit `skip` from a check does NOT cascade to dependents
  (that has always been the case), so `github-release` must self-gate on the
  tag itself, not depend on `remote-tag` skipping.

Alternative considered: keep the checks as-is and accept the extra WARN noise
and network calls. Rejected because of item 2 -- the `gh`-missing hard FAIL in
a previously-clean state is a real misbehavior, not just noise.

## Affected files

- `rlsbl/checks/release.py` (`check_remote_tag`, `check_github_release`)
- Possibly `rlsbl/data/checks.toml` if `depends_on` entries are adjusted
  (they can stay for ordering; they no longer gate on WARN)
- Tests for the release checks (add cases: tag absent locally -> remote-tag
  and github-release both skip; no gh binary + tag absent -> no FAIL)

## Effort

Small: two self-gating early returns plus tests.
