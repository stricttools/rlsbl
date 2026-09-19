# A release that aborts before its commit leaves an artifact stamped with the unreleased version

## Context

`rlsbl release run` writes generated artifacts before it writes any version
file. The strictcli schema dump writes `.strictcli/schema.json`, and
`selfdoc gen` runs right after it; both happen well before the step that
writes the new version into `package.json`, `pyproject.toml`, `selfdoc.json`
and `.rlsbl/version`, and before the release commit that carries them.

The schema dump is stamped with the version the run computed from the release
file's bump, not the version the repository holds at the moment it runs. So
between the dump and the release commit, the working tree holds a generated,
committed artifact describing a version that does not exist yet.

Observed during a live release: the run aborted at the selfdoc check, which
failed on two documentation pages whose frontmatter descriptions had gone
stale against their changed content. Nothing had been bumped and nothing had
been committed, which is the intended property -- everything above the
candidate push is reversible. But `.strictcli/schema.json` was left modified
in the working tree, reading the new version while every version file still
read the old one.

## Problem

The leftover artifact collides with the release protocol's own prescribed
invocation. `rlsbl release run --no-allow-dirty` is what the release
documentation tells an operator to run, and its clean-tree check refuses to
start while the tool's own leftover output sits in the tree. So "change
nothing and re-run" -- which is the correct outcome, since the re-run
regenerates the file at the right version and folds it into the version-bump
commit -- is not an available move.

That leaves an operator with three candidate actions and nothing anywhere
stating which is right:

- **commit it** -- wrong: an artifact carrying the next version inside a
  repository still at the previous one is what `version-consistency` reads;
- **revert it by hand** -- wrong: the file has a generator, and hand-editing a
  generated file makes the editor the generator;
- **regenerate it** -- right, and stated nowhere.

The abort message names none of the three, and does not name the file the run
wrote.

## Solutions

### Write the version-dependent artifacts after the version write

Move the schema dump, and any other pre-commit generated write whose content
depends on the new version, to after the step that writes the version.

- **Pros:** removes the residue instead of cleaning it up. An abort before the
  version write then leaves the tree byte-identical to HEAD, so there is
  nothing for an operator to adjudicate and no message to write. It also stops
  the working tree from ever carrying an artifact that describes a state the
  repository has not reached.
- **Cons:** the dump runs early so that a schema error fails the release before
  anything is bumped. Moving it trades that ordering for the residue property.
  Check first whether the dump can fail for a reason the bump itself would not
  have introduced; if it cannot, the early position buys nothing.

### Revert what the run generated, on abort

The run already knows the set of files it generated -- it prints
`Including hook-generated file: .strictcli/schema.json` when it assembles the
release commit. On an abort before the release commit, restore each generated
file the run wrote.

- **Pros:** covers every pre-commit generated write at once, not only the
  version-stamped one.
- **Cons:** a revert is a write into the operator's working tree performed by a
  command that is failing, and it must never touch a region the operator
  edited in the same file. Concurrent sessions share the worktree, so the
  revert has to be scoped to the exact content the run itself produced.

### Name the files and the remedy in the abort message

Have the abort print which files the run generated, say that a re-run
regenerates them, and name the command that restores them meanwhile.

- **Pros:** smallest change, and it satisfies the standing requirement that
  every error naming a remedy has that remedy executed once in a fixture
  before it ships.
- **Cons:** leaves the residue in place, so the clean-tree check still refuses
  the prescribed re-run and the operator still has to act on a tool-owned file.

## Affected files

- the release pipeline's pre-commit steps (the schema dump and the selfdoc
  generation) and its abort path
- the clean-tree check, where it meets files the tool itself wrote
- `docs/release-workflow.md`, whose step table states the ordering
- a regression test that aborts a run between the generated write and the
  release commit, then asserts both the resulting tree state and the message

## Effort

Small to medium, depending on the solution chosen. The reordering is the
smallest code change and the largest change in behavior.
