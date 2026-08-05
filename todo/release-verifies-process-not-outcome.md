# The release flow verifies the process, not the outcome

Five defects found while executing a coordinated multi-project release round. They share a
root: the release reports success on the basis of what it *did*, not on what actually
*happened*. Items 1 and 2 will hit every remaining consumer release; items 3-5 are narrower
but were each hit live in the same round.

## 1. `release run` exits 0 when the publish workflow goes red

### Problem

Under the main-as-candidate ordering, the release's own mutating phase ends at "tag pushed,
GitHub Release created". The actual registry publish happens afterwards and asynchronously,
in the Publish Router workflow. `--watch` monitors the **CI** Router, not the Publish Router,
so the command exits before the publish has an opinion and never revisits it.

Observed twice in one round, in two different repos:

- A Go project: tag created, GitHub Release created, Go module served by the proxy — and
  **zero binaries attached to the Release**, because both publish jobs died. `release run`
  exited 0.
- A monorepo member: tagged and Released, publish gate refused, nothing on either registry.
  `release run` exited 0.

Both were discovered only by querying `gh run list` by hand afterwards. A release that
half-succeeds and reports success is worse than one that fails, because nothing prompts the
operator to look.

### Solution options

- **(a) Extend `--watch` to the publish workflows.** Poll the Publish Router runs triggered by
  the tag; exit non-zero on any red job.
  - Pros: cheap; it is what `--watch` already implies; fast and specific failure signal.
  - Cons: trusts CI's self-report. A publish job can go green having uploaded nothing — which
    is exactly how the Go project above produced a Release with no binaries.
- **(b) Probe the registries after the publish settles (recommended).** For each target the
  release declared, ask the registry whether it now serves the new version; hard-error if not.
  Reuses machinery that already exists: `publication_probe()` on `BaseTarget` (built for
  `release yank`), the drift detection behind `status --registry`, and `ResolvedTarget`, which
  already knows which targets were in scope.
  - Pros: checks reality rather than a workflow's opinion; catches the green-but-empty case
    that (a) structurally cannot; consistent with the stateless-engine principle that reality
    (git + remote + registries) is the only state; the remedy is the existing `release retry`.
  - Cons: needs a bounded retry for index lag — one registry was observed running ~5 minutes
    behind a successful publish job.
- **(c) Both.** Watch for the fast signal, probe for ground truth.

Recommendation: (b), optionally with (a) as the fast path. Do not settle for (a) alone.

## 2. The scaffolded secret scan targets the source tree, not the artifact

### Problem

There are two secret scans and they disagree about scope. The release flow's own gate scans
**build artifacts** in `dist/`. The scaffolded publish workflow separately runs
`gitleaks dir .` across the **entire repository**, with no config file scaffolded alongside it.

Any repo whose tests plant realistic fake secrets — scrub tests, scanner tests, credential
handling tests — now fails its publish on strings that were never going to ship. One consumer
required a hand-written `.gitleaks.toml` plus a follow-up patch release before its binaries
could reach the Release at all. Most of the fleet has just been re-scaffolded and has not yet
released, so this is armed and waiting on roughly twenty repos.

### Solution options

- **(a) Scaffold a `.gitleaks.toml` stub** as a user-owned file (the `pre-checks.sh` pattern:
  created once, never overwritten), so allowlists survive re-scaffold.
  - Pros: makes the gate usable out of the box; allowlists live in-repo and are reviewable.
  - Cons: an empty stub still fails first on any repo with fixtures, and it teaches
    "add an allowlist entry" as the reflex response to a red gate.
- **(b) Scope the CI scan to what actually ships (recommended).** Scan the built artifacts,
  matching the local gate.
  - Pros: the threat model for a *publish* gate is "a secret leaked into a published
    artifact", and a fixture under `tests/` is not in the wheel. Zero false positives from
    fixtures while still catching the leak that matters. Removes the disagreement between the
    two scans.
  - Cons: a secret committed to source but never shipped goes uncaught by this gate. That is a
    real problem, but it is a repository/history problem wanting a pre-push or history scan —
    conflating it with the publish gate is what produced a gate that cries wolf.
- **(c) Drop the CI scan as redundant with the local gate.**
  - Pros: simplest; the local gate already hard-errors before push.
  - Cons: loses the backstop for anything that reaches CI without going through the local path.

Recommendation: (b). Add (a) on top only as an opt-in for repos that genuinely want source
scanning, never as a default that fails.

## 3. A batch release cannot resume a mid-flight member

### Problem

`monorepo release run` calls `run_cmd`, which hard-errors when an in-progress marker exists and
directs the operator to `release resume`. But `release resume` is per-project: resuming a
stalled member individually bypasses the batch's single-push discipline, its single shared CI
gate, and its archive gate. For a batch that stalled after `BRANCH_PUSHED`, the batch path is a
dead end — the only way forward abandons the guarantees the batch exists to provide.

Related, and hit in the same round: in-progress markers are untracked and not gitignored, so
`--no-allow-dirty` refuses to re-run until they are committed — making the documented
fix-forward loop unreachable without a hand commit first.

### Solution options

- **(a) Give the batch path its own resume route** that re-enters batch orchestration for
  pending members. Pros: preserves every batch guarantee; the obvious command keeps working.
  Cons: the resume logic must reconcile per-member markers with batch-level state.
- **(b) Make the batch's remediation text honest** — say plainly that resuming exits batch mode
  and what is given up. Pros: trivial. Cons: documents the gap rather than closing it.

Recommendation: (a). Track the marker gitignore/commit question with it, since both surface on
the same path.

## 4. A resumed release can have a structurally empty candidate window

### Problem

The CI gate now requires the releasing project's own CI check to have actually run on the
candidate (a skipped check no longer passes as green). That is correct. But a resume can reach
a state where satisfying it is impossible through no fault of the operator: when a batch
finalizes its other members serially, each finalize commit pushes the branch tip past the
stalled member's own version-bump commit. The resumed candidate's push diff then contains only
other projects' finalize commits, the member's paths filter matches nothing, its CI job is
skipped, and the gate — correctly — refuses to tag.

Hit live: a pending member's resume was in exactly this state, and the only honest way out was
to author an unrelated real change under that project's paths so the candidate would match.

### Solution options

- **(a) Include the member's own version-bump commit in the resumed candidate range**, so the
  push window always contains something matching its filter. Pros: closes the class at the
  source. Cons: needs care not to re-push commits already on the remote.
- **(b) Detect the condition before pushing** and refuse early with the remedy, rather than
  after a full CI wait. Pros: cheap; fails fast. Cons: still leaves the operator to construct
  a qualifying commit by hand.

Recommendation: (a), with (b) as the guard for any residual case.

## 5. The docs-site deploy pipeline: missing credentials and a vanishing error

### Problem

Two separate faults on the same path, both hit in one release:

1. The pipeline requires deploy credentials from the shared environment file, but only
   post-release *hooks* source it — pipelines do not. The deploy fails for want of credentials
   that exist on the machine.
2. The failure was invisible. The release exited 1 with the error swallowed by stdout
   buffering: the log ended mid-flow showing nothing at all. It was only diagnosed by running
   the pipeline in-process, unbuffered.

Fault 2 is the more general defect — any pipeline error can disappear the same way.

### Solution options

- Source the shared environment for pipelines as hooks already do, or declare the required
  credential names so a missing one is a named hard error rather than a downstream failure.
- Flush or unbuffer pipeline output so a failure is never lost to redirection. This should be
  fixed independently of the credential issue, since it hides every other pipeline error too.

## Affected files

- `rlsbl/commands/release/execute.py` — exit code, post-publish verification
- `rlsbl/commands/watch.py` — watch scope (item 1a)
- `rlsbl/targets/base.py` — `publication_probe()` reuse
- `rlsbl/commands/status.py` — existing registry drift detection to reuse
- `rlsbl/templates/**/publish*.yml.tpl` — scan scope (item 2)
- `rlsbl/commands/init_cmd.py` — optional `.gitleaks.toml` scaffolding
- `rlsbl/commands/monorepo/batch_release.py` — resume route, candidate range (items 3, 4)
- `rlsbl/pipelines/cloudflare_pages.py` — credentials and output buffering (item 5)

## Red-green expectation

Each item has a concrete observed failure shape to reproduce first: a release whose publish
workflow goes red must exit non-zero; a repo containing a fixture secret under `tests/` must
publish cleanly; a batch stalled after `BRANCH_PUSHED` must be resumable in batch mode; a
resumed candidate must contain a commit matching the member's own filter; and a pipeline
failure must appear in a redirected log.

## Effort

Medium — one focused visit and one patch release. Items 2 and 5 are small and mechanical;
item 1's probe and item 4's candidate-range change are the two that deserve real tests against
the failure shapes above. Sequencing note: items 1 and 2 gate any further consumer releases,
since every one of them will otherwise hit the same two traps.
