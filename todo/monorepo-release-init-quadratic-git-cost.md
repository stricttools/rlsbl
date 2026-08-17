# `rlsbl monorepo release init` spawns one git subprocess per (project, commit)

## Problem

`rlsbl monorepo release init` did not complete within 15 minutes on a workspace
with 53 registered projects and ~100 commits since the last releasable tag. It
wrote no `unreleased.toml` and printed nothing, even under `--verbose`. The
process stayed in state `R`, accumulating CPU steadily, so it was progressing
rather than deadlocked -- just far slower than a scaffolding step can be.

The cost model is visible in the code:

- `_cmd_batch_release_init` walks every registered project.
- Per project, `_get_unreleased_commit_count` resolves that project's own tag
  glob, takes `git log --format=%H <last_tag>..HEAD`, and hands the SHAs to
  `filter_commits_for_project`.
- `filter_commits_for_project` loops the SHAs and calls `get_commit_files(sha)`
  for each one.
- `get_commit_files` shells out to `git diff-tree ... <sha>` -- one subprocess
  per call, with no cache anywhere in the path.

So the subprocess count is `projects x commits-in-that-project's-range`. A
project that has never been tagged individually gets the whole history as its
range, so its contribution is `len(all commits)`, not `len(unreleased commits)`.
On the workspace above that is tens of thousands of `git diff-tree`
invocations, each paying process-spawn cost to read a file list that many other
projects in the same run will ask for again.

`filter_commits_for_releasable` has the same shape at the releasable level (its
docstring already notes it calls `get_commit_files()` once per commit and
matches in memory, which is the right idea one level up -- but the per-project
path underneath still re-reads the same commits).

## Why it matters here

This is the scaffolding step of the documented release protocol: the operator
is told to run `monorepo release init`, edit the file it writes, commit it, and
then release. When it does not finish, there is no release file to edit and the
protocol has no next step. Falling back to writing `unreleased.toml` by hand
works (the file is user-edited anyway, and an archived `batch-*.toml` shows the
schema), but that is a workaround for a step that is meant to be automatic.

The silence makes it worse than the slowness: nothing is printed, `--verbose`
adds nothing, and there is no progress indication, so the only way to tell
"working" from "hung" is to sample `/proc/<pid>/stat`.

## Possible directions

Listed as options, not a decision:

1. **Read every commit's file list once per run.** One `git log --name-only
   --format=%H <widest range>` (or one `git diff-tree` sweep) into a
   `{sha: [files]}` map, then match every project against that map in memory.
   Turns `projects x commits` subprocesses into one.
2. **Cache `get_commit_files` per process.** A plain `functools.lru_cache` (or a
   dict keyed by SHA) collapses the repeated reads across projects without
   changing any call site. Cheapest change; still one subprocess per distinct
   commit.
3. **Ask git to do the matching.** `git log --format=%H <range> -- <paths>`
   pushes the path filter into git, so no per-commit file list is needed at all
   for the common case. Watch globs that git's pathspec cannot express would
   still need the current path.
4. **Emit progress.** Whatever the cost, a scaffolding step that can run for
   minutes should say which project it is on, at least under `--verbose`.

Options 2 and 4 are small and independent of the others. Option 1 or 3 is the
structural fix.

## Affected files

- `rlsbl/commands/monorepo/batch_release_init.py` (`_get_unreleased_commit_count`,
  `_scaffold_releasable_sections`, `_scaffold_package_sections`)
- `rlsbl/git_util.py` (`get_commit_files`, `filter_commits_for_project`,
  `filter_commits_for_releasable`)

## Effort

Small for the cache plus progress output. Medium for the single-sweep rewrite,
mostly in making the watch-glob matching read from the prebuilt map.
