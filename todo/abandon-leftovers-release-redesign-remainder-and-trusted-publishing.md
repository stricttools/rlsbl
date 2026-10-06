# Abandon's leftovers, the rest of the interrupted-release redesign, and trusted publishing

Three groups of work, each independent of the others.

## 1. `rlsbl release abandon`: three leftovers

`rlsbl release abandon` records an abandoned release attempt's version as
never released, deletes `.rlsbl/releases/in-progress.json`, and commits the
archive. Three cases remain unresolved.

### 1a. A GitHub Release with no tag anywhere

When a GitHub Release exists for the version abandon would record, but no
tag exists locally or on the remote, abandon refuses and points at
`rlsbl release undo`. undo refuses any version without a release archive,
and `rlsbl release backfill` adopts tags only, so the operator is sent
between two refusals.

- **Open decision:** what abandon should name here. Candidates: deleting the
  stray GitHub Release (destructive, and it assumes nothing was published
  from it), teaching backfill to adopt a GitHub Release by the commit it
  names, or a refusal stating that the record and GitHub disagree and naming
  both facts. Whatever is chosen gets a red-green test that performs the
  named fix and asserts the refusal clears.

### 1b. A state file naming a version behind the latest release

When `in-progress.json` names a version lower than the latest release,
abandon refuses with the shared "behind the latest release" explanation but
names no fix.

- **Open decision:** which fix to name. The only candidate found is deleting
  the state file, which may discard a record someone needs. Investigate how
  this state can arise before choosing.

### 1c. The refusal for version files that cannot be ordered

Version files naming something that is not a version the record can hold
(for example `0.29`) are refused instead of crashing:
"0.29 is not a version the release record can hold (MAJOR.MINOR.PATCH,
optionally -alpha.N, -beta.N or -rc.N), so it cannot be ordered against the
latest release, 0.29.3. Nothing was changed. Fix the version files, then
re-run."

- **Open decision:** this refusal was added during the build without the
  owner's ruling. Confirm it, or replace it.

## 2. The rest of the interrupted-release redesign

`rlsbl release abandon` covers part of what a stalled release needs. The
remainder of the redesign was designed but not built:

- **Wire the step table fully.** `rlsbl/commands/release/steps.py` declares
  each release step with its inverse and its artifact probe, but only the
  first phase's dispatch reads it; the later steps are pointers into one long
  function, and undo still carries its own hand-written revert order. Make
  every step a callable the executor dispatches through the table, and have
  undo, rollback, and abandon walk the table.
- **Computed refusals.** Every refusal about release state lists the
  commands that are legal now, computed from the recorded steps and local
  artifacts, instead of prose written at each site, so the fix a message
  names can never disagree with what the state allows.
- **A read-only `rlsbl release status`** reporting the release in progress:
  its version, which steps completed, which artifacts exist locally, which
  commits arrived after the pin, and the legal next commands.
- **A committed attempt record** replacing the untracked state file, so a
  stalled release is a reviewable commit visible to every session and every
  machine, and a second attempt at a version is answerable from the
  repository alone.

- **Open decision:** whether all of this is still wanted now that abandon
  exists, or which part. The step-table wiring is the part every other piece
  reads from.

## 3. Trusted publishing in the generated publish workflows

npm restricts tokens that bypass two-factor authentication: they lost
sensitive account actions in August 2026 and lose direct publishing around
January 2027. Generated publish workflows still publish to npm with a
long-lived token. npm's trusted publishing proves a publish through the
GitHub Actions run itself, with no stored token.

- **Open decision:** whether family projects publish npm packages through
  trusted publishing or through a token with every package in scope. Under
  the current refinement, the remaining npm publishers are the npm wrappers
  of Go tools, and whether those keep publishing at all is also open.
- If trusted publishing is chosen: generate workflows that publish with
  OIDC and no token, document the one-time publisher registration on npm
  per package, and add a check that a project configured for trusted
  publishing carries no npm token secret.

## Effort

Group 1: small per case once decided. Group 2: large. Group 3: medium.
