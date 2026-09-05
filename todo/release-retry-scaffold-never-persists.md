# `release retry` claims to scaffold retry.toml but the file never persists

## Context

`rlsbl release retry` re-dispatches publish workflows for a completed
release and is driven by `.rlsbl/releases/retry.toml`, mirroring how
`release run` is driven by `unreleased.toml` (which `release init`
scaffolds for the user to edit).

## Problem

When `retry.toml` is absent, `release retry` prints that it auto-scaffolded
the file — but no file exists on disk afterward, and the command then
errors against the missing file it just claimed to have written. The user
is left to hand-write `retry.toml` from documentation. Observed live during
a real npm-publish retry: the printed scaffold message was followed
immediately by the missing-file error, and the retry only proceeded after
the file was created by hand.

Likely shapes: the scaffold is written to the wrong path, written and then
removed by cleanup, or only rendered to stdout while the code path that
persists it was never wired.

## Solutions

1. **Persist the scaffold and stop (most correct, matches `release init`).**
   On a missing `retry.toml`, write the scaffold to the real path, print
   where it was written and that it needs editing, and exit non-zero without
   dispatching anything. Identical contract to `release init`: the file is
   the consent artifact, the user edits and re-runs.
2. **Drop the auto-scaffold claim; print the template.** Error immediately
   with the template body in the message for the user to paste. Honest, but
   worse ergonomics than option 1 and inconsistent with the `init` pattern.

Per the red-green convention: first a failing test reproducing the
claims-scaffold-but-file-absent sequence (assert the printed claim and the
absent file together), then the fix, then the test asserts the file exists
with the expected scaffold content after the first invocation.

## Affected area

The `release retry` entry point's missing-file branch and its scaffold
writer; the command's tests.

## Effort

Small — one code path plus a regression test.
