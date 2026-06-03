# changelog add accepts commits outside the current sub-project

## Problem

`rlsbl changelog add` in a monorepo accepts any commit hash without validating that the commit touches files within the current sub-project's watch patterns. The validation only happens later in `check_in_range`, which filters commits by watch globs and rejects hashes that don't match. The guardrail is at read time (check) instead of write time (add).

This means a session doing bulk changelog coverage can accidentally add SDK commit hashes to framework's unreleased.jsonl, or root-project hashes to a sub-project's file. The entries pass schema validation and look correct, but `rlsbl check --tag changelog` later rejects them as "out of range."

## Observed in

gamehome monorepo: 87 cross-project hashes were added to the root project's unreleased.jsonl during a bulk changelog session. Framework sub-project had 18 more. All hashes were valid commits after the tag, but they touched files outside the sub-project's watch patterns.

## Fix

`rlsbl changelog add` should call `filter_commits_for_project()` (git_util.py:52) on each provided hash and error if the commit doesn't touch any files matching the current project's watch globs. This catches the mistake at write time, before the entry is appended to unreleased.jsonl.

## Affected code

The `add` command in `rlsbl/commands/changelog_cmd.py` (or equivalent). After resolving each hash, check it against the project's watch patterns before appending.
