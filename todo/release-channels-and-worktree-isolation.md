# Release channels, worktree isolation, and branch-aware coverage

## Problem

Multiple Claude sessions (or developers) sharing a worktree cannot release independently. The current model assumes one linear flow on main: commit, add changelog entries, release. When two sessions work concurrently, they block each other because:

- Releasing requires a clean working tree (the other session's uncommitted work blocks you)
- Changelog coverage checks all commits since the last tag on the current branch, including the other session's uncovered commits
- There's no way to isolate a fix, release it, and merge back without version/changelog conflicts

The deeper issue: changelog coverage and release readiness are enforced globally on all branches, when they should be properties of designated release branches only.

## Design: release channel branches

### Core model

Branches are divided into two categories:

- **Release channel branches** (e.g., main, beta, nightly): changelog coverage enforced, linear history enforced, releases happen here, version tags live here
- **Work branches** (everything else): no changelog coverage, no history constraints, free to commit/rebase/force-push

Changelog coverage becomes a property of release channel branches, not of all branches. Work branches are messy by nature. Release channel branches are clean by design.

### Configuration

New `release_channels` key in `.rlsbl/config.json`:

```json
{
  "release_channels": {
    "stable": {
      "branch": "main",
      "linear_history": true,
      "coverage_required": true
    },
    "beta": {
      "branch": "beta",
      "linear_history": true,
      "coverage_required": true,
      "version_suffix": "beta"
    }
  }
}
```

Each project defines its own channels with their own branch names and rules. The existing `release_branches` config key (currently used only for manual-push warnings) would be superseded by this.

### Pre-push hook changes

The pre-push hook currently enforces changelog coverage on every push regardless of branch. It needs to become branch-aware:

- Parse stdin to determine the target remote ref (git pre-push provides `<local ref> <local sha> <remote ref> <remote sha>` on stdin)
- Only enforce coverage when pushing to a release channel branch
- The current limitation noted in CLAUDE.md: "The hook does NOT pass `$@` to rlsbl" — this needs revisiting since stdin parsing is separate from CLI args

### Linear history enforcement

On release channel branches, no merge commits allowed between the last tag and HEAD. This ensures:

- `<last_tag>..HEAD` is an unambiguous ordered list of commits
- No "which parent did this tag come from?" ambiguity
- Tags sit on a clean chain
- Every commit is a distinct unit of work for changelog coverage

Implementation: a new rlsbl check (`--tag history` or similar) that verifies `git log --merges <last_tag>..HEAD` is empty on release channel branches.

### How code enters a release branch

With linear history enforced, regular `git merge` (which creates merge commits) is forbidden on release branches. Options:

- **Rebase + fast-forward**: preserves individual commits, each needs coverage
- **Squash merge**: one commit per feature, one changelog entry — simpler but loses granular history
- **`rlsbl promote` command**: new command that handles the mechanics

## Design: rlsbl promote

New command that moves commits from a work branch to a release channel branch. This is the gate where discipline is enforced — not during daily work.

```
rlsbl promote --to stable       # promote current branch to the stable channel's branch
rlsbl promote --to beta          # promote to beta channel
```

The promote command:

1. Identifies the target release branch from channel config
2. Rebases work branch onto the target (or squash-merges — configurable)
3. Validates changelog coverage for all new commits being introduced
4. If coverage is incomplete, lists uncovered commits and asks for entries (interactive)
5. Fast-forward merges the target branch
6. Optionally runs `rlsbl check` on the result

## Design: rlsbl hotfix

Dedicated workflow for releasing fixes independently of in-progress work.

```
rlsbl hotfix start [--from <tag-or-ref>]   # create worktree + branch
rlsbl hotfix list                            # show active hotfix worktrees
rlsbl hotfix finish                          # merge back + cleanup
```

### Worktree management

`hotfix start`:

1. Determines base: defaults to the latest release tag, `--from` overrides
2. Creates a branch: `hotfix/<description>` or auto-generated from the todo/issue
3. Creates a git worktree at `../<repo>-hotfix-<branch>` (sibling directory)
4. Runs `uv sync` in the worktree to create a local venv (so `uv run rlsbl` uses the worktree's source)
5. Prints instructions: cd path, use `uv run rlsbl` for commands
6. Optionally registers the worktree in `.rlsbl/worktrees.json` for tracking

`hotfix finish`:

1. Validates: all commits have changelog entries, release was successful
2. Cherry-picks the code fix commits (NOT version bump, NOT changelog finalization) to main
3. Removes the worktree: `git worktree remove`
4. Deletes the hotfix branch (local and remote) if fully merged

### Editable install in worktrees

The main worktree has rlsbl editable-installed globally. A hotfix worktree needs its own isolated rlsbl. Options considered:

- `uv run rlsbl` from inside the worktree uses the local pyproject.toml and source — simplest, no global state changes
- Reinstalling globally from the worktree (`pip install -e <worktree>`) — disrupts the other session
- Shell wrapper/alias created by `hotfix start` — fragile

Recommendation: `uv run rlsbl` in worktrees. The `hotfix start` command prints this as part of its instructions. No magic.

### Version conflicts between hotfix and main

Hotfixes from a tag are always patch bumps (v0.73.0 -> v0.73.1). Main moves forward with minor/major bumps (v0.74.0). No collision because they occupy different version slots.

When the hotfix is merged back to main, main's next release bumps from the highest existing tag. So if v0.73.1 exists and main is at v0.73.0, `rlsbl release run` on main with a minor bump goes to v0.74.0 (bumping from the project's version file, which is still 0.73.0 on main).

Edge case: what if someone hotfixes twice? v0.73.1, v0.73.2. Main's version file still says 0.73.0. The release command bumps from the version file, not from the highest tag. So main's minor bump: 0.73.0 -> 0.74.0. The hotfix tags (v0.73.1, v0.73.2) coexist in the tag namespace without conflict.

### Changelog isolation in hotfix worktrees

The hotfix branch starts from a tag. `<last_tag>..HEAD` only includes hotfix commits. The worktree has its own `unreleased.jsonl` that only covers those commits. No contamination from main's in-progress work.

After `hotfix finish` cherry-picks the fix to main, main's `unreleased.jsonl` needs an entry for the cherry-picked commit. This could be:
- Manual: the user adds the entry
- Automatic: `hotfix finish` adds a non-user-facing entry for the cherry-pick pointing to the hotfix release for details
- Semi-automatic: `hotfix finish` copies the relevant entries from the hotfix's released JSONL, remapping commit hashes to the cherry-picked ones

## Design: release channels (nightly/beta/stable)

### Version scheme

Channels use semver pre-release suffixes:

- Nightly: `0.74.0-nightly.20260616`
- Beta: `0.74.0-beta.1`
- Stable: `0.74.0`

Tags follow the same scheme: `v0.74.0-beta.1`, `v0.74.0`.

### Promotion between channels

Promotion means: the exact code that was released as beta.1 becomes stable. No rebuild, no re-test (optionally re-test). Just re-tag and re-publish.

```
rlsbl release promote --from beta --to stable
```

This creates a new stable tag pointing to the same commit as the beta tag. Publishes to stable registries.

### Changelog behavior across channels

- Beta releases: changelog entries exist but are marked as pre-release
- Stable release: changelog consolidates all beta entries into the stable version
- Nightly: no changelog entries (too frequent, auto-generated summary at most)

### Per-project configuration

Not all projects need all channels. Configuration is per-project:

- Most projects: just `stable` (equivalent to current behavior)
- Libraries with consumers: `beta` + `stable`
- Actively developed tools: `nightly` + `beta` + `stable`

## Open questions

### Q1: Should work branches have unreleased.jsonl?

Options:
- **No**: changelog entries are only created during promote. Work branches are pure code. The promote command is where you add entries.
- **Yes, optional**: work branches can have entries for tracking, but they're not validated. Promotes carries them over.
- **Yes, required before promote**: you add entries as you work, promote validates them. This is closest to current workflow but scoped to promote time.

Pros of "no": cleanest separation, no stale entries on abandoned branches. Cons: you have to remember all your changes at promote time.

Pros of "yes, required before promote": closest to current discipline, entries are written while context is fresh. Cons: entries on abandoned branches are wasted work.

### Q2: Squash merge vs rebase for promote?

- Squash: one commit per feature branch, one changelog entry. Simpler. Loses per-commit granularity.
- Rebase: preserves individual commits. Each needs coverage. More granular changelog.
- Configurable per-project: `promote_strategy: "squash" | "rebase"` in channel config.

### Q3: How many channels in MVP?

- **Just stable** (what most projects already do, but now explicit in config). This gives us branch-aware coverage and linear history without the full channel machinery.
- **Stable + beta**: adds pre-release versioning and promotion.
- **Full nightly/beta/stable**: adds automated nightly releases.

Recommendation: MVP is "just stable" — it solves the immediate problem (branch-aware coverage, hotfix isolation) without the pre-release complexity.

### Q4: Should hotfix finish auto-merge to main?

- Auto-merge: less friction, but risky if main has diverged significantly.
- Cherry-pick: safer, only brings the fix code. But the user needs to add a changelog entry for the cherry-picked commit on main.
- Manual: `hotfix finish` just cleans up the worktree and tells you what to do. Safest but most friction.

### Q5: How does this interact with monorepo releases?

Monorepo projects share a git history but have independent changelogs and versions. Release channels would need to be per-sub-project or per-workspace. The promote and hotfix commands need to be monorepo-aware.

### Q6: What happens to the existing release_branches config?

Currently `release_branches` is a list of branch names that trigger manual-push warnings. It would be superseded by `release_channels` which carries more information (channel name, linear history, coverage). Migration path: auto-convert `release_branches: ["main"]` to `release_channels: {"stable": {"branch": "main"}}` during scaffold.

### Q7: Should linear history be enforced or just recommended?

Enforced means `rlsbl check` and pre-push reject merge commits on release branches. Recommended means a warning only. Enforcement is stronger but blocks workflows that rely on merge commits (e.g., GitHub's "merge pull request" button creates merge commits).

This interacts with how PRs are merged: squash-merge and rebase-merge produce linear history; merge-commit does not. Projects using GitHub PRs with merge-commit would need to switch to squash or rebase merge.

## Affected areas

- `.rlsbl/config.json` schema (new `release_channels` key)
- Pre-push hook (`rlsbl pre-push-check`) — branch-aware coverage
- `rlsbl release run` — restrict to release channel branches
- New command: `rlsbl promote`
- New command: `rlsbl hotfix start/list/finish`
- `rlsbl check` — new tags for linear history, channel validation
- `rlsbl scaffold` — generate channel config, update hooks
- Monorepo support for all of the above

## Effort estimate

- **MVP (branch-aware coverage + hotfix start/finish)**: Large. 2-3 releases worth of work. Changes pre-push hook, adds hotfix commands, adds channel config.
- **Full channels (beta/stable/nightly + promote)**: Very large. 4-6 releases. Pre-release versioning, promotion logic, changelog consolidation, registry-specific pre-release support.
- **Linear history enforcement**: Small-medium. Standalone check that can ship independently.

## Related

- `todo/handle-empty-remote-first-push.md` — the immediate bug that surfaced this design. The empty-remote fix is a prerequisite for hotfix worktrees (releasing from a new branch that hasn't been pushed).
