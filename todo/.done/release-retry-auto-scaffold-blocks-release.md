# release retry auto-scaffolds retry.toml into working tree, blocking re-release

## Problem

When `rlsbl release run` fails at the push step (e.g., `push_timeout` not configured), it correctly rolls back the version bump and tag. However, running `rlsbl release retry` auto-scaffolds a `retry.toml` file into `.rlsbl/releases/`. This file is untracked, making the working tree dirty, which blocks subsequent `rlsbl release run` attempts with "working tree is not clean."

## Steps to reproduce

1. Run `rlsbl release run --watch --yes` in a project without `push_timeout` in config.json
2. Release proceeds through version bump, tag, but fails at push with `push_timeout not configured`
3. rlsbl rolls back version bump and tag (correct behavior)
4. Add `push_timeout` to config.json, commit
5. Run `rlsbl release retry --watch --yes`
6. Retry scaffolds `.rlsbl/releases/retry.toml` (untracked file)
7. Run `rlsbl release run --watch --yes`
8. Fails: "working tree is not clean" because retry.toml is untracked

## Expected behavior

Either:
- `release retry` should not scaffold retry.toml into the working tree if it can't proceed (or should gitignore it)
- `release run` should ignore untracked files in `.rlsbl/releases/` when checking for clean working tree
- `release retry` should clean up after itself if it fails to parse the scaffolded file

## Context

Hit this in gamehome. The release succeeded locally (version bump, selfdoc, tests, tag) but failed at push because `push_timeout` was missing from `.rlsbl/config.json`. After adding push_timeout, the retry mechanism created a blocking untracked file.
