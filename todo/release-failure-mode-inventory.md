# Release-failure-mode inventory from a real three-attempt release

A consumer project ran a large release that took three attempts to reach
the registries (CI failures on the release commit each time — all
legitimate test failures, so every gate behaved correctly). The saga
surfaced a set of distinct problems in the release flow itself. Per
request this todo describes problems only — no proposed solutions.

## 1. All local mutations happen before CI has ever run

Commit, tag, push, GitHub Release creation (with goreleaser binaries
attached), JSONL changelog finalization (files renamed and chmod 444), and
the release-file finalization all complete BEFORE the first CI result
exists. When CI then fails, the world already contains: a pushed tag, a
public GitHub Release page whose attached binaries were built from a
commit whose tests fail, an immutable finalized changelog for a version
that effectively doesn't exist, and registries still on the old version.
The publish gate correctly refuses npm/PyPI — but everything visible on
GitHub asserts the release happened.

## 2. Every failed attempt permanently burns a version number

Three consecutive failures consumed 0.X.0, 0.X.1, 0.X.2; the published
release is 0.X.3. Consumers see version gaps; the changelog contains three
finalized version files whose versions were never installable; each orphan
needed its own manual `release deprecate` invocation. Nothing in the flow
anticipates that a version might need to be abandoned pre-publish.

## 3. The publish gate's rlsbl-ci-sha marker was never visible

The Publish workflow's gate retried five times looking for the
`rlsbl-ci-sha` marker in the release body, never found it, and fell back
to `$GITHUB_SHA`. The fallback happened to be correct, but the primary
mechanism silently failed on every attempt observed.

## 4. The failure-remediation guidance is wrong for code failures

The gate's error message instructs: "fix the failure, re-run the CI
workflow to green on this exact commit, then re-dispatch the publish
workflow at the tag ref." When the failure is IN the code at the tagged
commit (the common case), re-running CI on that exact commit can never go
green, and the instruction is unfollowable. The actual remedy (new fix
commit, full new patch release, deprecate the orphan) is undocumented at
the point of failure. `release retry` also cannot help in this case (it
re-dispatches workflows for the same commit).

## 5. Recurring doc-check failure inside the release's own preflight

The release flow's own preflight (schema dump + selfdoc gen + selfdoc
check) regenerates files whose content legitimately changes on every
release (a generated CLI index embeds the new version number), which
trips the doc-staleness check and ABORTS the release. The operator must
commit the preflight's own regeneration artifacts and baseline-accept,
then re-run. This fired on the very first attempt and required manual
intervention on every subsequent release. The release flow fails on
changes the release flow itself makes.

## 6. Default timeouts are below observed real-world costs

The push step exceeded the default push timeout (large release, slow
remote), and the pre-push check suite (a -race test run measured at
~105s) exceeded the 120s default check timeout. Both had to be raised via
environment variables mid-release. Defaults that a real project exceeds
in the normal case are miscalibrated.

## 7. The detached CI watcher does not surface outcomes

`--watch-async` starts a detached watcher writing to a local-only log.
The three CI failures were discovered by manually querying the forge, not
by the watcher; nothing about the watcher's outcome reaches the operator
unless they remember to inspect its log file.

## 8. No isolation from concurrent work on the release branch

While one release attempt was in flight, a concurrent session pushed
unrelated commits (a todo file and its changelog entry) onto the release
branch. The commits rode into the next attempt's release unreviewed. The
release range is computed at run time from the current branch state, so
anything that lands between attempts silently joins the release.

## 9. Changelog auto-commits interleave with in-flight work

`changelog add`'s auto-commit moves HEAD. During interleaved work this
caused an amend to land on the wrong commit (the changelog auto-commit
instead of the intended code commit), requiring a manual unwind. The
auto-commit behavior and multi-step working sessions compose badly.

## 10. Orphan cleanup is manual, per-version, and easy to skip

After a publish-gate refusal there is no marker anywhere that a version is
an orphan. Discovering that three GitHub Releases pointed at
failed-CI commits, deciding yank does not apply (nothing reached
registries), and running deprecate three times was all manual operator
judgment. A less careful operator would have left three
legitimate-looking broken releases as the repo's public face.
