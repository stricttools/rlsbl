# `release undo`: incomplete unwind (peel-loop bug), no audit journal, no published-release guard

## Context

`release run` creates up to three commits before tagging: the version-bump commit (message = tag string), `chore: finalize changelog for <v>`, and `chore: finalize release file for <v>` (commit order per `commands/release/execute.py:1140,1206`, tag created at `execute.py:1261` — so the tag points at the release-file finalize commit, not the version-bump commit). `release undo` is expected to unwind all of these.

## Problems

### 1. The peel loop can only ever revert one commit in real git

`undo.py:286-318` peels commits by reading HEAD's subject, reverting, then re-reading HEAD's subject:

- After `git revert --no-edit HEAD`, HEAD is now the newly created `Revert "chore: finalize release file for <v>"` commit.
- That subject matches neither the changelog-finalize regex (`undo.py:302`) nor the expected tag-string message (`undo.py:309`), so the loop exits after a single revert.
- The `elif any_finalize_reverted:` branch (`undo.py:313-316`) then reports success ("finalize commit(s) only") and the push proceeds.

Consequences observed in a consumer project: only the release-file finalize commit was reverted; the version-bump commit and changelog-finalize commit survived, leaving all version files at the undone version, the versioned JSONL still finalized (read-only), and CHANGELOG.md still showing the undone version — a state that neither `release run` nor a second `undo` can cleanly proceed from.

The existing test `test_undo_handles_finalize_commit_at_head` (`tests/test_undo.py:195-238`) hides this: the mocked `git log -1` returns the *original* subject after the revert (line 211), i.e. the mock pretends `git revert` does not create a new HEAD commit. All undo tests fully mock `run`/`run_gh`, so real git semantics are never exercised.

### 2. The latest-release undo path writes no audit journal

Only the `--version` (non-latest) path writes `.rlsbl/undo-audit.json` (`undo.py:569-582`, via `evidence_gate.write_undo_audit`). The default path — the one that deletes tags and GitHub Releases — records nothing. After an incident there is no machine-readable record of what was deleted, which SHAs the tags pointed to, or what was reverted.

### 3. No guard against undoing a published release

Undo selects its target purely via `git describe --tags` (`undo.py:167-169`) and never checks whether the release was actually published to registries (e.g. whether the Publish workflow for that tag succeeded). Consecutive undos therefore walk backwards through history deleting the tags and GitHub Releases of *published* versions — destroying provenance for artifacts that are live on registries and (for Go) breaking module resolution for consumers.

## Solutions

1. **Fix the peel loop**: before reverting anything, walk the history from the tag target and collect the SHAs/subjects of the release commits to unwind (release-file finalize, changelog finalize, version bump); then revert each collected SHA explicitly (`git revert <sha>`), never re-deriving state from HEAD subjects mid-loop. Assert afterwards that version files no longer contain the undone version (hard error if they do).
2. **Audit journal on every path**: write `.rlsbl/undo-audit.json` (tag name, tag target SHA, GitHub Release id/notes snapshot, reverted SHAs, pushed refs, timestamp) in the latest-release path too, *before* executing deletions, so recovery information survives even a partial failure.
3. **Published-release guard**: before deleting anything, determine whether the target release published successfully (Publish workflow conclusion for the tag's SHA, or registry probes as in `release yank`). If published, refuse with a hard error directing the user to `release yank`/`deprecate` — undo is for failed/unpublished releases. No bypass flag.
4. **Real-git test fixtures**: convert the undo suite from fully-mocked `run` calls to temporary real repositories (mock only the `gh`/network boundary), so revert semantics, tag deletion, and multi-commit unwinding are tested against actual git behavior.

These belong together: (1) is the correctness fix, (2) and (3) are the safety rails that bound the blast radius when something else goes wrong, (4) is what keeps all of it true.

## Affected files

- `rlsbl/commands/undo.py` (peel loop `:286-318`, audit `:569-582`, target selection `:167-169`)
- `rlsbl/evidence_gate.py` (`write_undo_audit`)
- `tests/test_undo.py`, `tests/test_undo_releasable.py`

## Effort

Medium-high — a day including the real-git test conversion. The published-release guard needs a small design decision (workflow-conclusion check vs registry probes) consistent with how `release yank` already probes registries.
