# reconcile: repairing an old GitHub Release steals the "Latest" badge

## Context

GitHub awards a repository's "Latest release" badge to the non-draft,
non-prerelease Release with the most recent `published_at`. Editing a
Release's body leaves `published_at` alone, but the repair paths that
materialize or re-publish a Release set a fresh `published_at`.

## Problem

A machine-wide `rlsbl release reconcile` pass that touched historical
Releases re-published several v0.1.x-era Releases (their `created_at`
stayed original; `published_at` became the moment of the pass). GitHub
then displayed those ancient versions as "Latest" on six repositories,
until each was manually re-asserted with `gh release edit <real-latest>
--latest`. Nothing in the reconcile flow noticed or corrected this: the
pass reported success while silently changing which version every repo
advertises to visitors and to tooling that resolves "latest release".

## Proposed fix (either or both)

1. When creating or re-publishing any Release for a version that is not
   the repository's true latest, pass `make_latest: "false"` on the API
   call (supported on both create and edit), so the badge can never move
   as a side effect of repair.
2. As a belt-and-suspenders invariant: at the end of any reconcile apply
   that touched Releases, determine the true latest (highest released
   version per the release archives — the archives, not the tag
   namespace, are the authority) and re-assert it with
   `make_latest: "true"`. This also heals repos damaged by earlier runs.

Option 1 prevents the defect; option 2 makes the pass self-correcting.
Both fit the existing reconcile principle that a Release is rewritten in
place rather than deleted and recreated.

## Red-green test

Fixture: a repo with two Releases where the older one is then repaired by
reconcile. Red: the badge (the `releases/latest` API answer) moves to the
older version. Green after fix: the badge stays on the true latest. Per
the remedy-truthfulness rule, the test should assert via the same
`releases/latest` endpoint a visitor's badge reflects.

## Affected area

The reconcile Release materialize/re-publish path and its GitHub API
calls; possibly shared Release-writing helpers used by `release edit` and
`release scrub` if they can hit the same re-publish behavior on old
versions.

## Effort

Small: one parameter on existing API calls plus the end-of-pass
re-assertion and its fixture test.
