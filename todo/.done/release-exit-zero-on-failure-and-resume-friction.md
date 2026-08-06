# Release commands exit 0 on failure, plus three resume-path friction points

Found during a real monorepo releasable release that required four attempts.
Filed separately from the stale-plan todo because the defects are independent.

## 1. `release run` / `release resume` exit 0 after a hard failure (serious)

Every one of these printed a hard `Error:` and did no release, yet the process
exit code was **0**:

- `Error: working tree is not clean. Commit your changes first.`
- `Error: a previous release is in progress (v0.13.0, 4/14 steps completed...)`
- `Error: CI never ran for this project on the release candidate for 0.13.0.`
- `Error: selfdoc check failed`

An operator (or an agent) that trusts the exit code concludes the release
succeeded. The only reliable signal is to independently check `git tag` and the
registry after every run, which defeats the purpose of an exit code.

This is especially dangerous combined with the stale-plan defect filed
separately, where a run that does nothing also prints a success-shaped message.

**Fix:** any path that prints `Error:` and aborts the release must exit
non-zero. Worth an audit of every `print(..., file=sys.stderr)` + `return` in
the release command tree, and a test asserting nonzero exit for each terminal
error class.

## 2. `in-progress.json` is untracked, non-gitignored, and blocks its own resume

`release run` writes
`.rlsbl-monorepo/releasables/<name>/releases/in-progress.json` and deletes it on
success. When a release fails after `BRANCH_PUSHED`, the file remains as
untracked, non-gitignored working-tree content.

The next `release run` then fails its own clean-tree check on the very file it
just wrote:

```
Error: working tree is not clean. Commit your changes first.
```

So the documented recovery path is unreachable until the operator manually
resolves a file rlsbl itself created. Committing it is wrong (it is transient
state rlsbl deletes on success); deleting it strands the release.

**Fix:** exclude the release state file from the clean-tree check (it is
tool-owned, and the check exists to catch *user* changes), or have scaffold add
it to `.gitignore`. Excluding it from the check is better — it needs no repo
change and cannot be defeated by a missing gitignore entry.

Workaround applied downstream: gitignoring the path.

## 3. Regenerating changelogs permanently destroys a released version's description/context

`release run` regenerates per-version `changes/<v>.md` and the releasable
`CHANGELOG.md` on every attempt. A released version's **release description and
context** are not stored anywhere durable:

- the JSONL entries carry only per-entry `description`/`type`, never the
  release-level description/context
- the release-level text lives only in `unreleased.toml`, which is consumed and
  archived at release time

So every regeneration re-derives an already-released version's `.md` *without*
its description and context, silently deleting prose that cannot be recovered
except from git history. Observed concretely: a prior version's summary
paragraph and `<details>Context</details>` block were dropped from both
`changes/<v>.md` and `CHANGELOG.md`, and re-dropped on each subsequent release
attempt after being restored by hand.

**Fix:** persist the release-level description and context at finalization —
either as a header record in the version's JSONL, or as a sibling
`changes/<v>.meta.json` — and have the generator read it. Released changelog
output should be reproducible from committed state alone.

## 4. Path-filtered CI makes the release CI gate unsatisfiable after a scoped fix-forward

The generated `ci-router.yml` gates each member job behind a `dorny/paths-filter`
`detect` job. The release CI gate requires each member's job to have **passed**
on the candidate; `skipped` counts as not-run:

```
Error: CI never ran for this project on the release candidate for 0.13.0.
  a2a: a2a-ci / test: skipped
  ... (every member) ...
```

This is reachable through completely normal use: the first candidate touches all
members (the version bump edits every member manifest), CI goes red for one
member, the operator fixes *only that member* and resumes — and now the new
candidate touches one path, so every other member is skipped and the gate fails.
The fix-forward loop the error text recommends is exactly what triggers it.

The error message is excellent and self-diagnosing (it names the paths-filter
cause and the remedy), so this is a design tension rather than a silent bug. But
the remedy it suggests ("commit a change under one of this project's paths")
means fabricating a no-op change for 25 packages.

Note the generated filters already include the releasable `CHANGELOG.md` in
*every* member's filter list, which is an effective "run everything" hook — the
resolution used downstream was to land a real changelog fix, which tripped all
filters at once. Making that hook explicit and documented (or having the gate
accept `skipped` for members whose code is unchanged since the last *passing*
run) would remove the friction.

## Effort

1 is small and mechanical, and is the highest value — a wrong exit code makes
every other failure mode harder to see. 2 is small. 3 is moderate (a storage
format decision). 4 is a design discussion.
