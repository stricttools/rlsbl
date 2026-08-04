# Batch releases tag a commit the CI router's paths filter can hide

## Context

In a monorepo batch release, `rlsbl monorepo release run` releases each
releasable in turn. Per releasable it commits the version bump, regenerates
`.rlsbl-monorepo/snapshot.json` as a **separate `snapshot` commit**, and pushes
the candidate untagged. After the last candidate is pushed it gates CI once on
the final candidate SHA and then tags **every** releasable at that same SHA.

The generated `ci-router.yml` uses `dorny/paths-filter` to decide which member
projects' CI jobs run on a commit. Each project's filter is built from its
`watch` patterns in `workspace.toml` (plus its own path and, as the generator
already emits, its releasable `CHANGELOG.md`).

## Problem

The commit that a batch release ends up tagging is the **last `snapshot`
commit**, whose entire diff is `.rlsbl-monorepo/snapshot.json`. No member
project's generated paths filter matches that file, so on exactly the commit
that every tag points at, the router's `detect` job reports `false` for every
project and each project's CI job concludes `skipped`.

The publish gate then refuses to publish, correctly and by design:

> A CI check run matching the filter was SKIPPED. The gate cannot treat a
> skipped check as passing: this project must actually run its own CI on the
> release commit.

Result: a batch release that passes every local preflight silently fails to
publish for some or all of its members, **after** the tags and GitHub Releases
have already been created. The repo is left in a half-published state that
`release retry` cannot fix, because the underlying commit will never gain a
non-skipped CI run.

Which members survive is pure luck: a project publishes only if its own release
commit happened to ride in the same push window as the final candidate, so its
suite had already run on the tagged commit for an unrelated reason.

## Evidence

Observed on a three-releasable monorepo (one PyPI, one Go, one npm) doing its
first batch release. All three tags landed on the same bare `snapshot` commit:

```
$ git show --stat --format="%H%n%s" <tagged-sha>
<tagged-sha>
snapshot

 .rlsbl-monorepo/snapshot.json | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
```

CI Router run on that commit:

```
detect:                    success
<npm-proj>-ci / test (22): success
<npm-proj>-ci / test (24): success
<py-proj>-ci / test:       skipped
<go-proj>-ci / test:       skipped
```

Publish gate for the PyPI member (and identically for the Go member):

```
Publish gate: waiting for CI on <py-proj>@v0.1.0 (commit <tagged-sha>)
##[error]Publish gate: CI did not pass on <tagged-sha> -- refusing to publish.
  <py-proj>-ci / test: skipped
A CI check run matching the filter was SKIPPED. ...
```

Net outcome of that release: npm published, the Go module proxy served the tag
(Go "publishing" is just a VCS tag, so it resolved despite its gate failing),
and **PyPI was never published at all** — the package did not exist on the
index. Two of three publish workflows red, tags and GitHub Releases already
created.

The consumer worked around it by hand-adding to every project's `watch` list in
`workspace.toml`:

```toml
watch = [
    "<own-path>/**",
    ".rlsbl-monorepo/snapshot.json",
    ".rlsbl-monorepo/releasables/<name>/**",
]
```

With only that change and no other difference, the next batch release ran all
three suites green on the tagged `snapshot` commit and all three members
published. That confirms the diagnosis exactly — and confirms that today the
fix lives in consumer-edited config rather than in the generator.

## Why the workaround is not the fix

- It is consumer-side. Every monorepo that ever does a batch release must
  discover this the same painful way, after a half-published release.
- It is silent when absent. Nothing in `rlsbl check --all` flags a workspace
  whose watch patterns cannot satisfy its own publish gate. All checks passed
  locally in the failing case.
- It over-triggers. Watching `snapshot.json` means every member's full suite
  runs on every snapshot commit of every release, including releases that do
  not involve that member.

## Solution options

### Option A — Tag a commit every member's filter already matches

Do not tag a bare `snapshot` commit. Either fold the snapshot regeneration into
the last release commit, or emit a final batch commit that touches one file per
participating member (or reorder so the tagged commit is a real release commit).

- Pros: fixes the root cause; no filter widening, so no over-triggering; every
  tag points at a commit whose CI genuinely exercised the tagged code; nothing
  for consumers to configure.
- Cons: touches the batch orchestrator's commit/push sequencing, which is the
  most delicate part of the flow; "one file per member" is slightly artificial.

### Option B — Generator emits the release-machinery paths

Have `ci_router.py` always append `.rlsbl-monorepo/snapshot.json` and
`.rlsbl-monorepo/releasables/<name>/**` to each project's filter, exactly as the
consumer patch does. The generator already special-cases the releasable
`CHANGELOG.md`, so this is the same idea carried to completion.

- Pros: small, contained, obviously correct; matches an already-established
  pattern in the generator; benefits every existing monorepo on regeneration.
- Cons: over-triggers (every member's suite runs on every snapshot commit);
  treats the symptom — the tagged commit still contains none of the code being
  certified, so the gate's guarantee is weaker than it looks.

### Option C — Gate the batch on the candidate, tag from the gate result

The orchestrator already gates centrally on the batch candidate before tagging
("Deferring the CI gate to the batch orchestrator"). Make the per-release
publish gate trust that verification explicitly rather than independently
re-deriving CI status from the tagged commit's check runs.

- Pros: removes the double-verification entirely; the batch already knows CI was
  green on the candidate.
- Cons: weakens the publish workflow's independence — the gate exists precisely
  so that publishing cannot happen without CI evidence discoverable from the tag
  alone; a dispatch-retry at a tag would have no batch context to trust.

### Option D — Add a workspace check

Ship a check that fails when a member's watch patterns would not match the
commit a batch release will tag.

- Pros: converts a post-tag failure into a preflight failure; complements A or B.
- Cons: not a fix on its own; needs to model the orchestrator's commit shape.

### Recommendation shape

A is the most correct: the tagged commit should be one every member's filter
matches for real reasons. B is the cheap immediate mitigation and could ship
first; D is worth adding regardless so the failure can never again be discovered
only after tags exist.

## Affected files

- `rlsbl/ci_router.py` — filter generation (Option B, Option D)
- `rlsbl/commands/monorepo/batch_release.py` — candidate push / gate / tag
  sequencing (Option A, Option C)
- `rlsbl/commands/monorepo/snapshot_cmd.py` — snapshot commit creation (Option A)
- `rlsbl/commands/monorepo/sync.py` — router regeneration entry point
- `rlsbl/checks/workspace.py` — new preflight check (Option D)
- `rlsbl/publish_gate.py` — gate semantics (Option C)

## Effort estimate

- Option B alone: ~1-2 hours including tests and regenerating fixtures.
- Option D: ~2-3 hours (needs a model of the tagged-commit shape).
- Option A: ~4-6 hours — sequencing change in the batch orchestrator plus
  integration coverage that asserts the tagged commit matches every member's
  filter.

A regression test should assert end to end that, for a synthetic multi-member
workspace, the commit the batch orchestrator tags is matched by every
participating member's generated paths filter.
