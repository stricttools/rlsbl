The `batch_size_entries` validation check rejects commits that appear in more than 2 JSONL entries. The CLAUDE.md documentation explicitly states: "One commit can appear in multiple entries (a commit that fixes a bug and adds a feature)."

The check should either be removed or made more lenient (e.g., max 3-5 entries, or configurable). Currently it blocks releases when a commit is legitimately referenced in both a non-user-facing batch entry and a user-facing feature entry.

Found in: strictcli python 0.4.1.jsonl (commit 8eca2ae appeared in 3 entries -- 2 non-user-facing batches + 1 user-facing fix entry). Fixed by manually deduplicating the released JSONL.
