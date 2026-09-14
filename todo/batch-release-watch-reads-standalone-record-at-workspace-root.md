# Batch release: the trailing CI watch reads the standalone release record at the workspace root

## Context

`rlsbl monorepo release run` completed a one-releasable batch release
normally: candidate pushed, CI green, changelog finalized, tag created,
GitHub Release created, publish dispatched, batch file finalized and pushed.
Its last step, "Watching CI for <candidate sha>...", then exited with an
error instead of watching:

```
Error: the release record is empty, but this repository has version tags: <workspace root>/.rlsbl/releases
  No release archive exists there, so the release record records nothing -- and
  yet the tag namespace carries tags parsing under this project's scheme
  ("v*"), which is what a project that HAS released and was never
  backfilled looks like.
  Matching tags: v0.38.1, v0.38.0, v0.37.2 (and others)
  ...
  Backfill the archives -- preview first, then write:
    rlsbl release backfill --dry-run
    rlsbl release backfill --approve-consequential
```

The workspace root has no `.rlsbl/` directory at all; the releasables' records
live under `.rlsbl-monorepo/releasables/<name>/releases/` and were complete.
The `v*` tags the message lists are the repository's own history from before
it was converted into a workspace, when it released as a standalone project
under the default `v{version}` scheme.

## Problem

The post-batch watch step resolves the release record as if the current
directory were a standalone project: it looks for `<cwd>/.rlsbl/releases`,
finds nothing, sees version-shaped tags, and raises the empty-record refusal
that exists to protect standalone repositories that were never backfilled.
In a workspace that refusal is wrong twice over: the record it should consult
is each released releasable's own archive set, and the tags it finds are the
converted history, which the releasables' transition records already explain.

The release itself is not harmed, but the command exits non-zero after
printing "Batch release complete", so a caller reading the exit code believes
the release failed, and the watch never runs.

Every workspace whose repository released standalone before conversion has
this shape.

## Reproduction shape

- A workspace (`.rlsbl-monorepo/workspace.toml`) with at least one
  releasable and complete per-releasable archives.
- Historical standalone tags `vX.Y.Z` on the same repository.
- No `.rlsbl/` directory at the root.
- `rlsbl monorepo release run --no-allow-dirty --watch --approve-consequential`
  from the workspace root: the release completes, the trailing watch errors.

## Solutions

### Resolve the watch through the batch plan (recommended)

The batch orchestrator already knows the candidate sha and every released
releasable (it wrote them into the plan file before releasing anything). The
trailing watch should take its inputs from that plan and watch the candidate
sha directly, never consulting a release record. Watching is a question about
CI runs on a commit, not about the release archives.

- Pros: removes the record read from a step that never needed it; the same
  shape for standalone and batch releases.
- Cons: none identified.

### Teach the record reader about workspaces

Make the empty-record refusal workspace-aware: at a workspace root, resolve
the record per releasable and treat root-level historical tags as explained
by the releasables' transition records.

- Pros: fixes every other command that might read the record from a
  workspace root.
- Cons: larger change to the reader; the watch step still performs a read it
  does not need.

## Affected areas

- The batch release orchestrator's trailing watch step and the plan file it
  writes.
- The release-record reader's empty-record refusal (only under the second
  solution).
- A red-green test: a fixture workspace with historical `v*` tags and no root
  `.rlsbl/`, batch release completes, the watch step runs instead of raising.

## Effort

Small under the recommended solution: the plan already carries the inputs.
