# Release steps verify their effects; resume re-validates; artifacts come from the released commit; Go validation resolves the released module graph

## Context

One release of a Go project surfaced five pipeline findings in a single run,
and an audit of the release code afterwards found the classes behind them plus
several adjacent silent degradations and a Go-specific parity gap between the
local pre-release run and CI. This file collects all of it in one place —
point fixes, the structural work, speculative items and open decisions — so it
can be planned as one body. Each item states the problem as observed in the
code, the options with pros and cons, the affected files, and an effort
estimate. Locations name functions rather than line numbers where the
function name is stable.

Two of the findings may already have their own todo files in this directory
(the tidy-induced `go.mod` misdiagnosis and the blind CI retry). This file
deliberately does not reference or supersede them; duplicates are resolved at
triage.

The consumer's rulings that shape the priorities here:

- Release-time local validation is authoritative, not advisory: the
  pre-release run should match CI's selection and environment. A non-fatal
  `pre_release` hook entry (an "advisory local lane") was considered and is
  not wanted.
- Every step whose effect can be checked must check it; a step whose effect
  cannot be checked says so rather than passing; nothing rolls back past the
  candidate push, and "unverified" is a reported, resumable state rather than
  an abort.
- No implicit defaults and no escape hatches: none of the fixes below may add
  a flag that downgrades a verification to a warning.

## Item 1. A tidy-induced `go.mod` change is reported as a concurrent change

**Problem.** `_LOCKFILE_SPECS` (`rlsbl/commands/release/execute.py`) keys the
Go entry on `go.sum` and runs `go mod tidy`. `build_phase_a_plan`
(`phase_a.py`) adds only the spec's lockfile path (`go.sum`) to the files the
release will commit, `_expected_dirty_files` derives the permitted dirty set
from that list, and `_Executor._do_guard_unexpected_files` subtracts it and
raises "possible concurrent change" for anything left. An untidy committed
`go.mod` is rewritten by the release's own tidy one step earlier and then
reported as a foreign edit. One release aborted five times on that
misdiagnosis.

**Options.**

- (a) A preflight check `go-mod-tidy` in `rlsbl/checks/project.py` beside
  `check_dep_locks`, registered for Go targets with the `project` and
  `preflight` tags: run `go mod tidy -diff` (Go 1.23 and later; a floor is
  needed, or a scratch-copy comparison for older toolchains) and error before
  any mutation, naming `go mod tidy` plus a commit as the remedy. Pro: the
  release never mutates on an untidy manifest; cheap; the message is about
  the operator's situation. Con: another check to register; toolchain floor.
- (b) Declare `go.mod` as a path the sync step may write, so the guard permits
  it. Pro: one-line change. Con: an untidy `go.mod` is then silently tidied
  and committed inside the release commit — the exact silent degradation the
  fleet forbids.
- (c) (a) plus a guard message derived from step declarations (Item 10): when
  the unexpected path is one a step in the same plan could have written, say
  "step X rewrote path Y beyond its declared writes" instead of "possible
  concurrent change".

**Recommendation.** (c); (a) alone is the immediate fix.

**Affected files.** `rlsbl/checks/project.py`, `rlsbl/checks/__init__.py`
(`CHECK_TARGETS`), `phase_a.py` (`_do_guard_unexpected_files`).

**Effort.** Small.

## Item 2. A failed CI failure-log fetch degrades to a blind retry

**Problem.** `_fetch_failure_log` (`rlsbl/commands/watch.py`) reads job logs
through `gh api .../jobs/<id>/logs`. In `_watch_single_run`, any exception
from that fetch sets the classification to `"unknown"` — the code's own
comment calls this the historical blind retry — and `_retry_workflow` reruns
with no evidence. A second, quieter path: a fetch that returns an empty string
is classified `"infra"` by `_classify_failure` and rerun with `--failed`, also
with no evidence. The same class appears in `build_go_assets`
(`rlsbl/pipelines/build.py`), which falls back from goreleaser to a host-only
`go build` on failure — a cross-compiled matrix silently replaced by one
binary.

**Options.**

- (a) Fetch the failed-step text with `gh run view <run_id> --log-failed`,
  keeping `fetch_run_jobs` for job names and conclusions. Pro: the endpoint
  that answers the question asked. Con: reintroduces the repository-level log
  collection the current docstring warns can 404; needs a probe.
- (b) No evidence is a hard stop: a fetch that fails or returns nothing ends
  the watch with the exact `gh` command the operator can run, and never
  retries. Pro: no blind action ever. Con: interrupts every watch whose fetch
  fails, so it is only tolerable once (a) makes the common case succeed.
- (c) Both, in that order, plus a distinct `"no-evidence"` verdict from
  `_classify_failure` so an empty log can never read as `"infra"`; and the
  goreleaser fallback becomes an error.

**Recommendation.** (c), (a) before (b).

**Affected files.** `rlsbl/commands/watch.py` (`_fetch_failure_log`,
`_classify_failure`, `_watch_single_run`), `rlsbl/pipelines/build.py`.

**Effort.** Small to medium.

## Item 3. The local install and the built assets come from the live worktree, not the released commit

**Problem.** `GoPipeline.publish` (`rlsbl/pipelines/go.py`) runs
`go install <path>` in the project directory. At that step HEAD sits two or
three finalization commits above the tagged commit and the release's own
`.rlsbl/releases/in-progress.json` is present and untracked. Go's build stamp
(`debug.ReadBuildInfo`) therefore reports a pseudo-version such as
`vX.Y.Z-0.<timestamp>-<sha>+dirty`, and a tool that prints its version from
the build info reports that instead of the released version. Reproduced in a
scratch repository: the same shape yields the pseudo-version; removing the
untracked file removes only the `+dirty` suffix. `build_go_assets` builds the
release assets from the same live tree, and a consumer's own post-release hook
that runs `go install .` inherits the defect.

**Options.**

- (a) Install from the published module: `go install <module>@v<version>`
  with the public proxy, after the existing proxy notification (`go list -m`)
  proves the version is fetchable, under the same delay budget as the
  publication probe. Pro: the installed binary is the published bytes. Con:
  proxy lag; a private module needs a different route.
- (b) Build every artifact from a detached export of the recorded candidate
  commit (`git worktree add --detach <tmp> <candidate_sha>` or `git archive`)
  — the release already records `candidate_sha` and computes
  `release_commit_tree_hashes`. Pro: applies to assets as well as the
  install; no network dependency. Con: goreleaser configuration resolution in
  a detached worktree; cleanup on failure paths.
- (c) Both: assets from (b), install from (a). Plus: write
  `in-progress.json` outside the worktree, or gitignore it through the
  scaffold, so the release's own state never dirties the tree it measures.

**Recommendation.** (c).

**Affected files.** `rlsbl/pipelines/go.py`, `rlsbl/pipelines/build.py`,
`rlsbl/commands/release/publish.py` (`_upload_assets_for_config`), the
scaffold's gitignore template.

**Effort.** Medium.

## Item 4. Resume skips validation entirely

**Problem.** `_resume_cmd_inner` (`rlsbl/commands/release/__init__.py`) goes
straight to `_run_release_mutating`; its own comment says the validation phase
is skipped by design because it already ran. The whole preflight block — the
pre-checks hook, the schema dump, selfdoc gen and check, the selfblog
post-generate step, the selfdoc auto-commit, tests, lint, the pre-release hook
— is unreachable from resume. Only the CHANGELOG regeneration is redone. After
a fix-forward commit that edits documentation, the docs ship with content
hashes selfdoc never accepted and words its dictionary never saw.

**Options.**

- (a) Call selfdoc gen and check from resume, next to the CHANGELOG block.
  Pro: fixes the reported case. Con: a hand-picked pair; the next skipped
  step is the next incident.
- (b) Extract the preflight block into one `run_preflight(...)` shared by the
  fresh path and resume; on resume, re-run a declared set of cheap idempotent
  steps always, and the expensive ones (tests, lint, hooks) only when
  `git diff --name-only <candidate_sha>..HEAD` is non-empty. Pro: one code
  path for both entry points. Con: the extraction is real work (the block
  reads a dozen locals); the selfdoc auto-commit is not idempotent and would
  be flagged by the foreign-commit guard on a re-run, so it must be made
  re-entrant first.
- (c) Derive the re-run set from a per-step `idempotent` axis (Item 10)
  instead of a hand-declared list.

**Recommendation.** (b) now, (c) once the step protocol exists.

**Affected files.** `rlsbl/commands/release/__init__.py`, `validate.py`.

**Effort.** Medium.

## Item 5. Non-fatal post-release steps never verify their effect

**Problem.** Two independent swallows. A script-based post-release hook can
exit 0 after swallowing its own failure (a consumer's hook did so with
`|| echo "Warning: ... (non-fatal)"`), and rlsbl records `POST_HOOKS_RUN` as
success — the tool cannot see inside the script. A config-driven post-release
command is non-fatal through `fatal = "post-release" not in hook_name` in
`run_release_hook` (`rlsbl/commands/release/hooks.py`), so its failure is a
bare stderr warning inside `run_config_hooks` and never a failure marker,
while a script-based hook failure does mark the step failed — an asymmetry.
Nothing anywhere checks whether the docs site moved, whether the assets are on
the release, whether the deploy served. One consumer's docs site was three
releases stale before anyone noticed. Adjacent: `_upload_assets_for_config`
swallows a failed `gh release upload` as a warning yet `ASSETS_UPLOADED` is
marked complete though it is in `FATAL_STEPS`; `_do_sync_lockfile`
(`phase_a.py`) swallows a failed sync as a warning.

**Options.**

- (a) Symmetric fatality: config hooks raise like script hooks, and the
  existing `except` around post-hooks records `save_step_failure`. Pro: one
  behavior for both hook forms; the run exits nonzero and stays resumable.
  Con: does nothing for a script that swallows its own failure; nothing
  verifies an effect.
- (b) Per-step effect predicates. Each post-release step declares how its
  effect is checked: a `hooks.post_release_verify` config entry (for example
  a URL fetch asserting the served version), `gh release view <tag> --json
  assets` for the upload, the deploy target's own probe for deploys, the
  existing registry probe for publication. A failed check is recorded with a
  third marker kind beside success and failure — `save_step_unverified` —
  reported by name in the completion summary, exiting nonzero and leaving the
  state file for `rlsbl release resume`; nothing is rolled back. A step with
  no statable predicate reports "not verifiable" rather than passing, which is
  the rule `_probe_publication` already follows for targets without a probe.
  Pro: answers the actual question (did the effect happen) and cannot be
  swallowed by a script, because rlsbl evaluates it. Con: predicates that
  reach the network need timeouts, a retry budget and an inconclusive verdict;
  a flaky predicate reds a good release, so the inconclusive verdict is
  mandatory; `is_state_complete` and `get_missing_steps` must learn the new
  marker.
- (c) Generalize `_verify_publication` into that family rather than adding a
  parallel mechanism.

**Recommendation.** (b) built as (c). (a) is a stepping stone that (b)
supersedes.

**Affected files.** `rlsbl/commands/release/hooks.py`, `execute.py`
(`_verify_publication`, the post-hooks block, the completion epilogue),
`publish.py`, `phase_a.py`, `release_state.py`.

**Effort.** Large.

## Item 6. `require_tool(..., fatal=False)` turns declared release steps into no-ops

**Problem.** In release paths, an absent tool silently skips the step:
selfdoc gen and check (`validate.py`), the selfdoc call in
`pipelines/cloudflare_pages.py`, and siblings. A project that declares the
artifact (a `selfdoc.json` is present) and lacks the binary ships without
the docs step and without saying so.

**Option.** When the project has declared the corresponding artifact, an absent
tool is a hard error naming the install command; when it has not, the step is
not part of the release at all. Pro: no silent skip. Con: releases on
machines missing a tool go red — the intended pressure.

**Affected files.** `validate.py`, `pipelines/cloudflare_pages.py`, any other
release-path `require_tool(fatal=False)`.

**Effort.** Small; a behavior change that needs a changelog entry.

## Item 7. Go validation runs under whatever `GOWORK` the shell holds

**Problem.** Nothing in rlsbl knows `go.work` exists. The built-in Go test
step (`rlsbl/testing.py`: `go test ./... -race -short -count=1`), `go mod
tidy`, `go build` and every hook inherit the operator's environment. A
developer working with a gitignored `go.work` that overlays a sibling checkout
resolves a module graph CI never sees: the local run can be red for a reason
CI cannot reproduce (probed: a sibling's newer transitive dependency with a
changed API broke the local build while CI was green) or green on code CI
cannot build (an overlay carrying unreleased behavior). The PyPI side already
has a version-skew guard (`_abort_on_version_skew` in `validate.py`, reading
`dev-sources.toml.local-only`); Go has no analogue. Separately, the built-in
Go test step passes `-short` and the Go CI template
(`rlsbl/templates/go/ci.yml.tpl`) ships `go test ./... -race -short
-timeout=10m`, so the scaffold itself carries a local/CI selection difference
into every Go project.

**Options.**

- (a) Run every built-in Go step with `GOWORK=off` so the candidate is
  validated against the released module graph. Pro: the local verdict can no
  longer be wrong for an overlay reason; one environment variable. Con: hides
  from the operator that their overlay is broken (a `rlsbl dev` concern, not a
  release concern).
- (b) A Go version-skew guard: refuse the release when a `go.work` at or above
  the project overlays a module whose local version is ahead of the proxy's
  latest, mirroring the Python guard. Pro: shipping against unreleased
  dependency behavior becomes impossible. Con: needs proxy queries per
  overlaid module.
- (c) Both, and align the selection: drop `-short` from the built-in Go test
  step and from the Go CI template, add a timeout to the built-in step. The
  consumer's measurement: `-short` saves under five seconds on a five-minute
  suite and skips a wall-clock test plus most of a property test's cases.

**Recommendation.** (c).

**Affected files.** `rlsbl/testing.py`, `rlsbl/templates/go/ci.yml.tpl`,
`validate.py` (beside `_abort_on_version_skew`).

**Effort.** Small to medium.

## Item 8. A customized `pre_release` hook silently disables the built-in tests and lint

**Problem.** `is_hook_customized` (`hooks.py`) marks a non-empty
`hooks.pre_release` as customized, and the release then skips its built-in
test and lint steps entirely. The assumption is that the hook tests itself;
nothing states it at run time, and a consumer discovered that its three hook
commands were the whole local test surface only by reading rlsbl's source.

**Options.**

- (a) Print, at the point of skipping, that the built-in steps are skipped
  because the hook is customized. Pro: trivial. Con: a message agents ignore.
- (b) Require the declaration: a customized `pre_release` must be accompanied
  by an explicit `builtin_tests: false` (or the built-ins still run), with no
  implicit default. Pro: the skip is a stated decision. Con: a config change
  for every project with a customized hook.

**Recommendation.** (b).

**Effort.** Small.

## Item 9 (open decision). `hook_timeout` absent means no timeout

**Observation.** `get_hook_timeout` (`rlsbl/utils.py`) resolves to `None` when
the config key is absent, so a hung hook — including the whole local test run
under Item 8 — hangs the release forever, while CI carries its own timeout.
Whether the key becomes required (the no-implicit-defaults rule) or a
documented absence is an open decision.

## Item 10 (structural). The post-CI half of the release as a plan of typed steps

**Problem.** Phase A already went through a plan/executor split (`PlanStep`,
`build_phase_a_plan`, `_Executor`, `_STEP_HANDLERS` in `phase_a.py`), which
is what made it renderable under `--dry-run` and guardable. The post-CI half
— `_run_release_mutating` in `execute.py`, from `CI_VERIFIED` through
`POST_HOOKS_RUN` — is one straight-line function of roughly seventeen hundred
lines with resume handled by scattered `if step in completed` branches, and
`FATAL_STEPS` in `release_state.py` and the 27-step table in
`docs/release-workflow.md` are two hand-maintained lists describing it.

**Options, in increasing depth.**

- (a) Extend the Phase A machinery over the post-CI steps: each becomes a
  `PlanStep` with a kind, a payload, captured values (`verified_sha`,
  `pushed_sha`, `release_created` are plain locals today) and its markers;
  resume becomes one filter over the plan. Pro: creates the one place where
  a precondition, a postcondition, a fatality class and a resume policy can
  attach; removes the "step ran but its marker says otherwise" class by
  construction; makes `--dry-run` able to render the post-CI half. Con: the
  largest mechanical change in the tool, across the irreversibility boundary;
  the Phase A seam test (no undeclared reads in the executor) must extend to
  code that reads git freely today.
- (b) A step protocol with mandatory axes, mirroring the target protocol
  (`rlsbl/targets/protocol.py`): `precondition`, `postcondition` (Item 5's
  predicate), `verifiable`, `idempotent` (Item 4's re-run set), `fatality`
  (replacing `FATAL_STEPS`), `resume_policy`, `writes` (Item 1's derived
  guard message). A step that cannot answer an axis is an import-time error.
  The step table in `docs/release-workflow.md` is generated from the registry
  into a committed data file with a freshness check, as `support-matrix.json`
  and `target-matrix-fresh` already do for targets. Pro: obligations cannot
  be omitted; the documented pipeline cannot drift from the executed one.
  Con: some steps are singular (the CI wait, the candidate push) and a
  uniform protocol can bury their special reasoning.
- (c) Verification at the effects chokepoint: every mutating call through
  `rlsbl/effects.py` carries its step and resource; the executor snapshots the
  world around each step and asserts observed delta is within declared
  writes, naming the step and the undeclared path otherwise. The concurrent-
  change guard stops being a special step. Pro: every step's local effects
  are verified, not only the post-release ones. Con: snapshot cost on large
  repositories; tool caches writing inside the tree need a write-side
  allowlist like the observe-side one.
- (d) Evidence in the release archives: unify `evidence_gate.py`'s
  `Evidence` protocol with the archives' three-fate model so each step
  records the evidence of its own postcondition (CI verdict and run ids, tag
  and commit, GitHub Release id, each registry's answer, each asset's digest,
  the installed binary's self-reported version, the docs site's served
  version, each hook's exit and predicate answer) into the version's archive;
  `unverified` joins the fates; `rlsbl release reconcile` re-probes the
  recorded claims and reports drift, which is what would have found a stale
  docs site on the next release rather than never. Pro: "done" means recorded
  evidence; absence of evidence is a queryable, later-repairable state. Con:
  an archive schema (strictspec is the document authority), a backfill for
  every archived version, and a reconcile with opinions beyond git.

**Recommendation.** (a) then (b) are the prerequisites for everything above
Item 5; (c) and (d) are the destination. None of it is required for Items
1–8, which are the fixes to ship first.

**Effort.** Very large, in stages.

## Item 11. `_sync_lockfiles` is a superseded duplicate

**Problem.** `_sync_lockfiles` (`execute.py`) duplicates the Phase A lockfile
sync, is still exported from the package `__init__`, and is exercised only by
tests. Delete it and its tests, or make the Phase A step the single authority
it already is.

**Effort.** Small.

## Item 12 (speculative). A scaffold lint for hooks that swallow their own failure

**Observation.** rlsbl cannot see a failure a hook script swallows. Item 5's
predicates are the structural answer; a cheaper companion is a scaffold-time
lint that flags `|| true` and `|| echo ...` on a command line inside a
scaffold-managed hook. Low priority.

## Item 13 (considered, not wanted). A non-fatal `pre_release` hook entry

An advisory local test lane (the hook runs fast and its verdict does not block
the release) was considered and rejected: the hook runs once per release, not
during development, so latency is not its purpose, and an advisory lane with
false reds is a lane nobody reads. Recorded here so it is not re-proposed
without new evidence.

## Open decisions

- Item 5: the exact marker vocabulary (`unverified` beside success and
  failure) and where the inconclusive verdict is surfaced.
- Item 7: whether the Go skew guard queries the proxy per overlaid module or
  reads the sibling checkout's own release record.
- Item 8: `builtin_tests: false` as the declared opt-out, or a different
  spelling.
- Item 9: `hook_timeout` required or documented-absent.
- Item 3: the placement of `in-progress.json` (outside the worktree, or
  scaffold-gitignored).

## Ordering

Items 1, 2, 6, 7, 8 and 11 are small and independent; ship them first. Item 3
next (it fixes what a consumer actually installs). Item 4's extraction of the
preflight into one function is needed by Item 10 anyway, so do it before the
larger work. Item 5 depends on Item 10(a) to have a place to attach
predicates, but its symmetric-fatality half can ship now.
