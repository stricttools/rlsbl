In the strictcli monorepo, the go sub-project's `unreleased.jsonl` accumulated entries referencing commits that predated the last release tag (`go/v0.1.1`). These stale entries caused `in_range` validation failures during release.

Root cause: entries were added to `unreleased.jsonl` by batch-covering cross-project commits, but were not cleaned up when the release finalized. The finalization step renames `unreleased.jsonl` to `x.y.z.jsonl`, but if entries reference commits from before the release tag, they carry over into the next cycle.

Suggested fix: during finalization, validate that all entries in the about-to-be-finalized JSONL only reference commits in the release range. Strip or warn about out-of-range entries.

Discovered during strictcli go-strictcli v0.2.0 release.
