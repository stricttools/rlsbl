# release init should handle stale unreleased.toml

## Problem

`rlsbl release init` errors with "unreleased.toml already exists" when a previous release was abandoned mid-flow. There is no `--force` flag and the error message doesn't suggest a remedy. The user must manually delete the file or overwrite it.

## Context

This happens when `release init` is run, the release file is edited, but `release run` is never called (session interrupted, user changed their mind, etc.). The stale file blocks future `release init` calls.

## Suggested fix

Either:
1. Add `--force` to `release init` that overwrites the existing file (with a confirmation prompt unless `--yes`)
2. Have `release init` detect the stale file and offer to overwrite: "unreleased.toml already exists. Overwrite? [y/N]"
3. Add `release reset` or `release clean` that removes stale release artifacts
