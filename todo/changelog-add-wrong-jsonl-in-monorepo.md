# changelog add writes to wrong JSONL in monorepo sub-projects

## Problem

`rlsbl changelog add` from a monorepo sub-project directory sometimes writes to the root `.rlsbl/changes/unreleased.jsonl` instead of the sub-project's JSONL file. The command reports success ("Added entry with 1 commit(s)") but the entry is missing from the sub-project's file, causing `rlsbl release run` to fail with "uncovered commit."

## Reproduction

```bash
cd /home/m/Projects/strictcli/python   # sub-project directory
rlsbl changelog add --commits f94f50b --no-user-facing --no-commit
# Output: "Added entry with 1 commit(s)"

grep f94f50b .rlsbl/changes/unreleased.jsonl
# Empty — not in the sub-project's JSONL

grep f94f50b ../../.rlsbl/changes/unreleased.jsonl  # or similar root path
# Found here instead
```

This happened multiple times in a single session. The pattern is inconsistent — some `changelog add` calls from the same directory correctly write to the sub-project JSONL, while others write to the root.

## Impact

Every occurrence requires manually editing the JSONL file to add the missing entry, then the release attempt fails again because the manual-edit commit is also uncovered, creating a cascading cycle.

## Expected behavior

`rlsbl changelog add` from a sub-project directory should always write to that sub-project's `.rlsbl/changes/unreleased.jsonl`. Never the root.
