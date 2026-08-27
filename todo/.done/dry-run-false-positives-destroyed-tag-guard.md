# `--dry-run` reports a destroyed tag for every already-released version

## Symptom

`rlsbl monorepo release run --dry-run` (and, by the same path,
`rlsbl release run --dry-run`) aborts with the destroyed-tag diagnosis on a
project whose current version *is* tagged, both locally and on the remote:

```
Error: version 0.41.0 appears to have been released before: its finalized
changelog .../changes/0.41.0.jsonl exists, but no tag "<name>@v0.41.0" is
present. This happens when the tag was deleted ...
```

`git tag -l "<name>@v0.41.0"` prints the tag. `git ls-remote --tags origin`
lists it. Nothing is wrong with the repository.

## Cause

`tag_exists_locally` (`rlsbl/utils.py`) runs `git tag -l <tag>` through the
effects handle and documents its own behaviour:

> Answers False when the question is unanswerable -- a preview past its first
> recorded mutation, where the framework replies to every observe with a stale
> carrier.

In a preview, the release pipeline records a mutation early (the `selfdoc gen`
step is enough), so by the time `compute_release_version` asks whether the
current version's tag exists, every observe is answered with an unsettled
carrier and the helper returns `False`.

`compute_release_version` reads that `False` as "first release", and
`_abort_on_destroyed_tag` then finds the finalized `<version>.jsonl` -- which
exists for every version that really was released -- and raises. So the guard
fires on exactly the healthy case it was written to distinguish from.

The helper's own rationale ("the tag is not there yet is the state a preview is
describing anyway") holds for the *collision* check further down
(`tag_exists_locally(tag)` for the NEW tag) but not for the
already-released check: there, `False` means "this version was never
released", which is a different claim and one the preview cannot support.

## Effect

`--dry-run` is unusable for previewing any release after the first on a project
that uses JSONL changelogs -- which is every project here. An agent previewing
a release gets a hard error naming a destroyed tag and a three-option recovery
procedure, none of which applies. The recommended recoveries are destructive-
adjacent (create tags by hand, or move the version forward), so acting on the
message makes things worse.

## Possible fixes

1. **Make the destroyed-tag guard preview-aware.** Skip
   `_abort_on_destroyed_tag` entirely when the answer that led here was an
   unsettled carrier rather than a real "no such tag". Requires
   `tag_exists_locally` to distinguish the two (return a tri-state, or expose a
   companion `tag_existence_known()`), instead of collapsing both onto `False`.
   Most correct: the guard then says what it means in both modes.
2. **Read tags outside the effects handle for this one question.** Listing tags
   is an observation with no side effect; the reason it goes through the handle
   is uniformity, not necessity. A direct read would make the preview answer
   the same question the real run answers. Cheapest, but it puts one git read
   outside the regime other reads follow.
3. **Take the first-release decision before any mutation is recorded.** The
   version/tag computation happens after `selfdoc gen`; moving it above the
   first recorded effect would make the observe settled again. Fragile -- it
   re-breaks the moment a step is reordered.

Option 1 is the real fix; option 3 alone is not a fix.

## Reproducing

Any monorepo releasable with a finalized `<current-version>.jsonl` and its tag
present:

```
rlsbl monorepo release run --no-allow-dirty --no-watch \
    --approve-consequential --dry-run
```

The non-preview run of the same command succeeds, which is the proof that the
repository state is fine.

## Affected files

- `rlsbl/utils.py` (`tag_exists_locally`)
- `rlsbl/commands/release/validate.py` (`compute_release_version`,
  `_abort_on_destroyed_tag`)

## Effort

Small for option 2, small-to-medium for option 1 (a tri-state plus every call
site that reads it, plus a regression test that previews a release of an
already-released version and asserts the preview renders instead of aborting).
