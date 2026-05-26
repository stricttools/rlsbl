# Release retry mechanism for missed CI

## Problem

When a GitHub Release is manually deleted and re-created (e.g., during CI debugging after a GitHub Actions outage), the release notes are lost. The re-created release has empty or default notes instead of the changelog section that `rlsbl release` originally attached.

`rlsbl edit-release` can restore the notes after the fact, but the user has to know about it and remember to run it. The broader problem is that there is no single command to handle the "release exists but CI never ran" recovery scenario.

## Current recovery workflow (manual)

1. Notice that CI/publish workflows never ran for a release
2. Delete the GitHub Release (to re-trigger the `release: published` event)
3. Re-create the GitHub Release manually (notes are lost)
4. Run `rlsbl edit-release` to restore notes from CHANGELOG.md
5. Optionally push an empty commit or use `gh workflow run` to trigger CI

This is error-prone and requires knowledge of multiple commands.

## Proposed solutions

### Option A: `rlsbl release retry` or `rlsbl release fix`

A dedicated command that handles the full recovery:

- Deletes the existing GitHub Release for the current (or specified) version
- Re-creates it with the correct release notes from CHANGELOG.md
- The `release: published` event fires again, re-triggering the Publish workflow
- Optionally triggers CI workflow via `gh workflow run` if no runs are found

This is a single command that replaces the multi-step manual process.

### Option B: Smarter `rlsbl watch`

Enhance `rlsbl watch` to detect the "no workflow runs found but release exists" case and offer remediation:

- If no CI/Publish runs are found after a timeout, check whether a GitHub Release exists for the tag
- If it does, suggest or offer to: re-create the release (to re-trigger Publish), manually dispatch workflows (if `workflow_dispatch` is configured)
- This turns a confusing timeout into an actionable diagnostic

### Option C: Both

Option A provides the explicit recovery command; Option B provides passive detection. They complement each other.

## Affected files

- New command: `rlsbl release retry` or `rlsbl release fix` (CLI registration, implementation)
- `rlsbl watch` (enhanced diagnostics for the no-runs case)
- Possibly `rlsbl edit-release` (could be absorbed into the retry command)

## Effort

Medium. The GitHub Release delete/re-create logic already exists across `rlsbl undo` and `rlsbl release`. The new command mostly composes existing pieces. The `rlsbl watch` enhancement is smaller but requires careful UX for the interactive prompts.
