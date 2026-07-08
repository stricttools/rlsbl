# Release-time schema dump ships stale schemas (silent + version-lagged)

## Problem

For strictcli-based projects, the release flow auto-runs the strictcli schema
dump to keep the committed schema file in sync. Two defects let a stale schema
ship without anyone noticing:

### 1. A failed dump is swallowed silently

The release invokes the dump via subprocess without checking the exit code (only
timeout/OS errors are caught). If the dump command fails for any reason — e.g. the
consuming CLI can't parse the dump flag — the non-zero exit is ignored and the
release proceeds with whatever stale schema was already committed. This was
observed in practice: a project shipped a release whose committed schema was two
versions behind because the dump had been silently failing.

### 2. The dump runs before the version bump

The schema embeds a `version` field sourced from the app's version. The release
runs the dump at an early step but writes the bumped version to the project files
at a later step. So at dump time the app still reports the *previous* version, and
the dumped schema is committed one release behind. Every release ships a schema
whose `version` lags by one.

## Impact

- Docs and downstream consumers that render from the committed schema show a
  wrong/old version.
- A structurally broken dump (exit non-zero) ships a stale schema with no signal.

## Suggested fixes

- **Hard-fail on a non-zero dump exit.** A failed schema dump must abort the
  release, not be swallowed. No silent degradation.
- **Dump after the version bump** (or re-dump after the bump) so the schema's
  `version` field matches the release being cut. Alternatively, normalize/omit
  the version field from the dumped schema so it can't lag (coordinate with
  strictcli — see the separate request there).

## Effort estimate

Small: add exit-code checking to the dump invocation and reorder (or duplicate)
the dump step relative to the version-bump step in the release flow.
