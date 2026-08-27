# An untidy committed `go.mod` aborts a release as a "concurrent change"

## Context

During `release run`, the lockfile-sync step runs one command per detected
ecosystem to bring the lockfile in step with the manifest. For Go that command
is `go mod tidy` (`_LOCKFILE_SPECS` in `rlsbl/commands/release/execute.py`), and
the spec is keyed on `go.sum` — `go.sum` is the file the release expects to be
modified.

`go mod tidy` does not only rewrite `go.sum`. When the committed `go.mod` is
itself untidy — a stale `require` line, a dependency no longer imported, a
missing indirect marker — tidy rewrites `go.mod` too. That file is not in the
release's expected set, so the next unexpected-modified-files check
(`_do_guard_unexpected_files` in `rlsbl/commands/release/phase_a.py`) sees a
modification it did not authorize and aborts:

```
Unexpected modified files detected (possible concurrent change): go.mod. Aborting release.
```

## Problem

The message names the wrong cause. It says "possible concurrent change", which
sends the operator looking for another session sharing the worktree — a hunt
that finds nothing, because the writer was the release's own tidy step one
phase earlier. The real cause (the committed manifest was untidy before the
release started) is never stated anywhere.

The diagnosis cost in one observed case: five aborted release runs, and
subprocess-level shims installed to intercept the child commands and see which
process actually wrote the file. Nothing in the release's own output points at
tidiness, and nothing points at the tidy step as the writer.

Two properties make this worse than an ordinary confusing error:

- The abort happens **after** the release has already begun mutating (version
  bump, generated docs), so each attempt leaves a working tree the operator
  must reason about before retrying.
- The condition is entirely knowable **before** any mutation: whether the
  committed `go.mod` is tidy is a question that can be asked at preflight, with
  nothing at stake in the answer.

## Solutions

### Tidy-check as a preflight with its own named error (recommended)

Add a preflight check for Go projects: run tidy in a scratch copy (or
`go mod tidy -diff` where the installed Go supports it), and if the committed
`go.mod` would change, fail before any mutation with an error that says exactly
that, and names the fix (`go mod tidy` and commit the result).

- Pro: the failure is stated in the vocabulary of the actual problem, and it
  arrives before the release has written anything, so there is no half-mutated
  tree to reason about.
- Pro: the same check works outside a release (`check --tag preflight`), so an
  untidy manifest is surfaced on any ordinary check run rather than only when
  someone tries to ship.
- Con: one extra Go invocation per release; needs a version floor or a
  fallback path for `-diff`, which is not in every Go release.

### Add `go.mod` to the expected set of the Go lockfile sync

Declare that the Go sync may write `go.mod` as well as `go.sum`, so the check
does not flag it.

- Pro: smallest change; the release stops aborting.
- Con: it silently commits a manifest rewrite the operator never asked for and
  never saw, inside a version-bump commit. That is the opposite of the
  declare-what-you-touch shape the rest of the release follows, and it would
  hide a genuinely broken manifest instead of reporting it.

### Keep the abort, fix only the message

When the unexpected path is one a sync step is known to be able to write,
say so: name the step that most likely wrote it and what makes it do that,
instead of asserting concurrency.

- Pro: cheap, and it fixes the actively misleading part.
- Con: still aborts mid-mutation, and still only after the release has spent
  its preflight and started bumping.

The first and third compose: a preflight that catches the ordinary case, plus
a check message that stops claiming concurrency when the modified path is one
the release's own steps can produce.

## Affected files

- `rlsbl/commands/release/execute.py` — `_LOCKFILE_SPECS`, `_target_lockfile_syncs`
- `rlsbl/commands/release/phase_a.py` — `_do_guard_unexpected_files`
- wherever preflight checks are registered, for the new check
- tests covering the Go lockfile sync path

## Effort

Small to moderate. The check itself is short; most of the work is deciding
where it registers and covering it with a test that commits an untidy `go.mod`
and asserts the named error rather than the concurrency abort.
