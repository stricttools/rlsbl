# Post-scrub integration: force push support and changelog SHA remapping

## Problem

After `safegit scrub` rewrites git history, rlsbl-managed repos are in a broken state:

1. **Can't push.** The rewrite changes all commit SHAs. `rlsbl release run` does a normal push, which is rejected because local and remote histories have diverged. The "never push manually" rule means there's no sanctioned way to sync the rewritten history. safegit already prints "This repository is managed by a release tool. Complete the rewrite via your release tooling" — but rlsbl has no mechanism to complete it.

2. **Changelog coverage is broken.** Every `commits` entry in JSONL changelog files (`.rlsbl/changes/unreleased.jsonl`, `.rlsbl-monorepo/releasables/*/changes/unreleased.jsonl`) references old SHAs that no longer exist. `rlsbl check --tag changelog` fails because `git rev-parse` can't resolve them. All released JSONL files (read-only, chmod 444) are also broken if their SHAs were in the rewritten range.

Both problems arise from the same root cause: safegit scrub rewrites commits but has no awareness of rlsbl's SHA-dependent metadata.

## What's needed

### Force push command for post-scrub scenarios

A command like `rlsbl push --force --reason "history rewrite from safegit scrub"` that:
- Does `git push --force` to sync rewritten history to the remote
- Skips the pre-push hook's changelog coverage check (the SHAs have changed, coverage can't be verified against old SHAs)
- Requires `--reason` (audit trail, no silent force pushes)
- Logs the force push event (reason, old HEAD, new HEAD, timestamp)
- Does NOT skip any other validation — it's a force push, not a release bypass

This is the only sanctioned way to push after a history rewrite. The normal release flow resumes after this push.

### Changelog SHA remapping after history rewrite

A command like `rlsbl changelog remap` that:
- Takes a SHA mapping (old SHA → new SHA) and updates all `commits` arrays in all JSONL files (unreleased and released)
- Or, if no mapping is available (post-GC, old objects gone), matches entries to new SHAs by commit message + author + timestamp
- Handles both standalone repos (`.rlsbl/changes/`) and monorepo releasables (`.rlsbl-monorepo/releasables/*/changes/`)
- Handles read-only released JSONL files (temporarily chmod, update, re-chmod)
- Validates that every remapped SHA resolves via `git rev-parse` after the update
- Auto-commits the updated JSONL files

### Ideal integration

The best outcome is that safegit scrub and rlsbl coordinate directly:
- safegit scrub detects rlsbl-managed repos (`.rlsbl/` or `.rlsbl-monorepo/` exists)
- During the rewrite, safegit builds an old-SHA → new-SHA mapping
- After the rewrite, safegit either: (a) outputs the mapping to a file that `rlsbl changelog remap` consumes, or (b) calls rlsbl directly to update the JSONL files as part of the same rewrite pass (so the changelog entries in the rewritten commits already have correct SHAs)

Option (b) is more correct — the JSONL files in historical commits would have the right SHAs from the start, not just on HEAD. But it requires safegit to understand rlsbl's JSONL format, which is tight coupling. Option (a) is looser — safegit outputs a mapping file, rlsbl consumes it post-hoc on the working tree.

## Severity

Blocking. Without these features, a history rewrite in an rlsbl-managed repo requires manual force push (violating the "never push manually" rule) and manual changelog repair (error-prone, tedious for repos with many entries).
