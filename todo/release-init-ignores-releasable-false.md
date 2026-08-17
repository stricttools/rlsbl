# `release init` does not honor `releasable = false`

## Context

`releasable = false` on a workspace project hard-errors every release path
(`release run`, `release edit`, `changelog add`, batch planning) — verified
across rlsbl/commands/release/validate.py:471, batch_release.py:988,
changelog_cmd.py:81, edit_release.py:64.

## Problem

`rlsbl release init` run from a non-releasable member project creates AND
auto-commits `.rlsbl/releases/unreleased.toml`, emitting only a warning about
explicit-releasable mode. The helper `_is_non_releasable_project` exists
(init_cmd.py:68-82) but is wired only into scaffolding (init_cmd.py:1801,
:2928), never into release init. The stray file then trips the
`releasable-residue` check and pollutes the tree. Observed live during a
release-hold probe; the artifact had to be saferm-deleted afterwards.

## Solution

`release init` hard-errors on a non-releasable project with the same sentence
family the other release paths use, before writing anything.

## Affected

- rlsbl/commands/init_cmd.py (or wherever release init's entry lives)
- a red-green test: init from a `releasable = false` member refuses and leaves
  no file

## Effort

Small.
