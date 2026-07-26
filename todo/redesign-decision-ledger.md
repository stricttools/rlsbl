# Redesign decision ledger (2026-07-26 design session)

A full-day design session reviewed the entire history of this project (all ~190 releases), inventoried
the whole fleet of consumer repos (27: 20 standalone, 7 monorepo workspaces, one repo carrying both
markers), surveyed real scaffold customizations, uncovered-path coverage, and env-var usage across the
fleet, and produced a ground-up redesign of rlsbl. The redesign was originally framed as a rewrite in
another language, but **almost none of the decisions are language-specific** — this ledger records every
decision so the current (Python) implementation can adopt them directly, without waiting.

Provenance legend, per the decision-provenance convention:

- **[%%]** = adopted from an AI recommendation ("Recommended" pick). Weakly held; freely reversible if
  evidence or reasoning goes against it. Never cite these back as deliberate user intent.
- Unmarked = user-directed or user-shaped decisions.
- **[OPEN]** = explicitly undecided; do not resolve without asking.

Deliberately excluded from this ledger: rewrite logistics (worktree layout, pilot binary naming,
big-bang migration mechanics, module paths, freeze policy). Those live in the design conversation and
apply only if/when the rewrite happens.

---

## 1. Core data model: releasable-first

1.1 **The releasable is the sole unit of release lifecycle** (version, changelog, tag, release state,
GitHub Release). A repo is a container of packages; packages may be grouped into releasables; a
standalone repo is the N=1 case — a default configuration, **not a separate mode**. There is no
standalone-vs-monorepo code path, no representative-member concept, no `.rlsbl/` vs `.rlsbl-monorepo/`
marker duality (one marker/state layout for all repos). [%%]

1.2 **The weak axiom (the enforceable form):** exactly one release-lifecycle implementation, operating
only on releasable objects; every point of variation (tag format, state dir, changelog prefix,
generated-workflow shape) is **per-releasable configuration resolved once at load time**. The runtime
never asks "am I standalone or monorepo?" — it asks "what is my tag format?". Specialness lives in
data, never in control flow. Falsifiable criterion: if a place is found where N=1 behavior cannot be a
load-time config default and needs a branch, the axiom fails there and that is a design event. [%%]

1.3 The strong axiom is explicitly **rejected**: uniform tags are impossible (Go module proxy requires
bare `vX.Y.Z` for root modules and `path/vX.Y.Z` companion tags for subdir Go members — companion tags
are part of the model, not an exception), and root releasables default to `v{version}` for tag
continuity. N=1 specialness survives as config defaults only.

1.4 **Implicit monorepo mode dies.** Per-package independent versioning without `[[releasables]]` is
removed; every package belongs to a releasable or is explicitly non-releasable. The three
implicit-mode workspaces in the fleet migrate by synthesizing one single-package releasable per
package (their existing `name@v*` tags already match).

1.5 **Mandatory workspace file, always.** Every repo — including all 20 single-releasable ones —
carries an explicit workspace file declaring its releasable(s) by name (state dirs, tag prefixes, and
gate regexes need the name; no derivation logic exists anywhere, no second mode). A single-releasable
repo's file is ~3 lines. [%%]

1.6 **Targets and pipelines both survive, cleanly redefined.** Target = package identity (ecosystem,
manifest path, version file) — exactly one per package path; needed even when nothing publishes.
Pipeline = a publish/deploy action referencing a target (or none, for targetless deploys). This is the
current model's shape, kept on purpose rather than by accretion. [%%]

1.7 **`dev_only` + `releasable = false` stay as an orthogonal pair.** Semantics pinned precisely:
`dev_only` constrains the *scope of inbound dependency edges* — the package may only ever appear in
other packages' dependency declarations in dev/test scope (`devDependencies`, `[dependency-groups]
dev`, `testImplementation`); runtime AND compileOnly-style inbound edges are banned (production code
referencing it at compile time is production coupling even if no bytes ship); outbound edges are
unrestricted. `releasable = false` = not versioned, no release lifecycle. They co-occur often but mean
different things. The name `dev_only` is kept **under protest** (the user dislikes it but no candidate
conveyed the meaning better; a genuinely better name may reopen this — do not bikeshed it again
without one).

1.8 **Releasable state self-containment invariant** (enforced by a check): everything describing a
releasable lives in exactly two places — its state dir and its one workspace-file section. Nothing
else in the repo may reference it. Consequence: the releasable is the portable unit; extraction is
`git mv` + move one section + tag continuity carried as data (`tag_format`). Package-level
extractability from a shared releasable is irreducibly a split and is deliberately not automated
(see 8.1).

## 2. File ownership and changelog coverage territory

2.1 **Total package partition with a single residual claimant.** Every workspace declares packages;
exactly one package per workspace holds the *residual claim* — it owns every tracked path no other
package claims (generalizing what `path = "."` root members already do). The residual claimant is
either releasable-assigned (root files get coverage) or `dev_only` (exempt) — an explicit per-repo
choice. Hand-enumerated `watch` glob lists **die**; territory is derived, not enumerated.

2.2 **No exemption vocabularies, no buckets, no exempt lists.** Earlier proposals (housekeeping-name
vocabularies, freeform exempt lists) were rejected: an exempt list adjacent to a coverage error
teaches agents the bypass. There is exactly one concept (package territory) and one existing
exemption mechanism (2.3). A path that is neither claimed nor residual-owned is a hard error:
"register this or claim it" — and registering/claiming leads INTO coverage or explicit dev status,
never out.

2.3 **The `Autogenerated: true` trailer exemption is retained** as the sole churn valve: tool-authored
commits (changelog finalization, lockfile sync, docs regeneration, scaffold) are coverage-exempt by
commit authorship, regardless of territory.

2.4 Empirical basis (fleet survey of all 8 workspaces): the uncovered population splits into benign
housekeeping (tool caches, generated docs, todo dirs, lockfiles, root manifests, READMEs — high
volume, correctly boring) and **real unregistered code** (an entire unregistered CLI package in one
workspace; native app code, a benchmark harness, and game prototypes in another; Terraform/systemd/
worker infra in a third; ~195 commits total with zero coverage anywhere). The partition rule exists to
catch the second population. Migration cleanup: register or claim every such path; add residual root
packages to the 5 workspaces lacking one.

## 3. State files and schemas

3.1 **All-TOML for human-edited state** (workspace file, config, release files, exclusion-free —
see 6); JSONL stays for changelog entries (append-oriented machine data). `config.json` dies as a
format. [%%]

3.2 **strictspec is the state layer.** Every rlsbl state file (workspace file, config, release file,
changelog JSONL, corrections) is defined as a strictspec schema: generated typed readers/validators
(strictspec targets Python as well as Go/TS, so this applies to the current implementation),
`format_version` gating with structured errors (got/expected/schema id/exact remediation), and
**breaking state changes ship as declarative strictspec migrations** instead of hand-rolled sweep
scripts. The workspace-file schema doubles as the formalized integration contract with strictcode
(which reads it). Accepted consequence: strictspec implementation is on the critical path of adopting
this layer. rlsbl's schemas are strictspec's first real consumer corpus.

3.3 **Schema evolution policy:** integer `format_version`, accept-exactly-current (no multi-version
reads, no compat paths — reading old versions is the banned pattern, the version field itself is
not); additive changes (new optional keys) never bump it; breaking changes bump it and ship with a
migration; mismatch is a hard error naming found-vs-expected and the remediation. A version field
does not make breaking changes non-breaking — it makes the failure precise.

3.4 **`released_by` declaration.** The workspace file carries a required `released_by` key naming the
exact binary that manages the repo. The binary hard-errors when invoked in a repo whose declaration
names a different binary. This is explicit mode selection (the caller declares its implementation),
not version pinning: repos declare *formats and identity*, never tool versions; a single always-latest
binary; mismatches are hard errors with forward-only remediation (move the repo forward, never hold
the binary back).

## 4. Release engine

4.1 **Revert-or-forward, no memory.** The engine is stateless; reality (git + remote + registries) is
the only state. Structure: [%%]

- **Phase 1 (pre-push, fully revertible):** validate → generate changelog → bump → commit → tag. Any
  failure auto-reverts completely (delete local tag, unwind release commits) — guarded by
  concurrent-commit detection so another session's interleaved commits are never destroyed. Re-runs
  start from zero. Nothing external happened, so nothing is remembered.
- **The single point of no return:** one atomic `git push` of branch+tag.
- **Phase 2 (post-push, forward-only):** GitHub Release creation, local pipelines, post-push actions —
  every step probe-retryable or idempotent by contract. Re-running the same release command probes
  ("tag on remote matches HEAD's version?") and completes only the leftovers. Probes must be
  lag-patient (GitHub read-replica lag is real; absent-just-created is retried, never trusted).
- **Beyond completion: fix-forward only.** A defective completed release is remediated by the next
  patch/infra release, never undone. Registries burn identifiers (PyPI rejects re-upload of a
  once-seen filename; npm forbids republish) — a half-published version is unrecoverable by design of
  the registries, so fix-forward is the only correct model.

4.2 **Crash-residue handling by content identity** (not heuristics): on startup the engine compares
found local residue (unpushed bump commit, local-only tag) against what the current inputs would
produce — adopt if it matches, refuse-and-halt if foreign. Requires deterministic-enough artifact
construction for the comparison (tree + message match; timestamps tolerated). [%%]

4.3 **Deleted outright:** `release resume`, `release undo` (both variants), `in-progress.json` and all
persisted release state, rollback-preservation machinery, batch plan sidecars. Batch re-runs skip
releasables whose tags are confirmed on the remote (probe, not ledger). The full idempotent-release
idea was evaluated a third time and rejected again for the same reason (registry identifier burn);
revert-or-forward is fix-forward formalized.

4.4 **Release file: exactly one repo-level `unreleased.toml`** with one `[releasable.<name>]` section
per releasable being released. Single-releasable repos have one section; batch is not a special case.
[%%]

4.5 **`infra` bump type stays** (it proves an infrastructure fix actually fixed CI/CD; the alternative
is fabricating user-facing entries, which is banned). `preid`/pre-release channels: **[OPEN]** — no
fleet usage, but non-usage reflects current single-developer/no-userbase conditions that must not be
assumed permanent; genuinely undecided, additive to add later.

4.6 **Pre-flight remote tag-collision check, explicit working directory on all git operations, and the
hardened release-push handshake** (bypass requires env var AND corroborating in-progress evidence)
are recent additions that carry forward as design facts.

## 5. Lifecycle extension (hooks are dissolved)

5.1 **There is no hook concept.** The pre/post-push split lands exactly on two existing typed seams:
[%%]

- **Pre-push extension = external-check entries**, extended with an explicit `kind`:
  `producer` (regenerates committed artifacts before validation — e.g. schema builds, docs gen) vs
  `gate` (read-only validation). This kills the mutating-check ambiguity.
- **Post-push extension = typed pipeline kinds**: `local-install` (refresh local binary),
  `docs-deploy`, `blog-publish` (the release-file blog field and body-file flow become its typed
  inputs), and `ssh-deploy` (see 5.4). Idempotency is inherited from the pipeline contract (post-push
  steps are re-driven by probe-retry), not asserted per entry.

5.2 The silent rule "customized pre-release hook disables built-in tests/lint" **dies**. Built-ins run
or don't per explicit config only. Shell hook script files (`pre-checks.sh` etc.) have no successor;
every observed fleet hook use maps onto the two seams (verified case-by-case).

5.3 **The freeform check kind survives, hard-declared**: `kind = "freeform"` is loud in config,
argv-based (no shell), timeout-bounded, hard-fail, declared producer-or-gate. Rationale: structured-
kinds-only would mean a per-sibling-tool catalog in rlsbl's source with every new integration gated
on an rlsbl release (or a disguised freeform via a generic subprocess kind). [%%]

5.4 **Deployment is elevated to a peer of registry publishing** (pending final confirmation of the
elevation framing): for service-shaped projects, release ≡ deploy — a release completes when all its
pipelines converge, and a server is just another kind of "world" alongside a registry. First deploy
kind: `ssh-deploy` (local build steps, remote steps, health checks, branch restriction), shaped
blue-green: the new version must pass health checks BEFORE the traffic switch, so a failed health
check means the switch never happened (phase-1 revert logic applied to deployment — no rollback
machinery). Deployment-as-convergence mirrors the mirror-reconciler pattern (observe → converge, plan
states, foreign-state tripwire). Evidence: one workspace had a real production deploy config
(cross-compile + rsync + systemctl swap) that was destroyed as collateral of the root-marker conflict
— removed for structural reasons, not by choice; the new model gives it a home again. rlsbl owns the
lifecycle slot and simple kinds only — it does not grow into an orchestration platform.

5.5 The standalone SSH deploy subsystem in its current form (config `deploy` key + `rlsbl deploy`
command as a separate engine) is superseded by 5.4; ad-hoc deploy invocation survives as running the
pipeline outside a release.

## 6. Changelog system

6.1 **Commit-based coverage only.** The `changeset-file` coverage mode is removed (zero adoption; its
motivating case never materialized). [%%]

6.2 **Batching discipline, inverted default:** one entry per commit is the baseline; a multi-commit
entry requires an affirmative group declaration (`group = {key, reason}`) in the entry itself.
**Deleted:** `max_commits_per_entry`, `max_entries_per_commit`, `--allow-batch`, config exclusion
records, and stale-exclusion cleanup machinery. A 13-commit overhaul is one entry with a declared
group; lumping unrelated commits now requires lying in a named field rather than being lazy. One
commit in multiple entries stays legal (each extra entry is already an affirmative act). [%%]

6.3 **Released-file immutability = plain writable files + content-delta validation** (option "C
strong"). chmod-444 and the unlock/rewrite/relock dance are **deleted** (git does not persist the 444
bit — every clone/checkout silently erases the deterrent — and the permission-leak bug class rides
along). Instead: released JSONL files are ordinary committed files; amend/edit/remap are atomic
rewrites via the tool; enforcement is a **content-delta check** validating against git history that
released files only ever change in whitelisted shapes — additive amend, field-level edit with stable
ULIDs, bulk SHA remap — gated at pre-push and CI. This is stronger than trailer-trust (validates what
changed, not who claims to have changed it). Scrub remains philosophically consistent: scrub rewrites
history, the changelog is part of history. [%%]

6.4 Git history is the audit trail; the Autogenerated trailer remains the authorship convention for
tool commits.

## 7. Scaffold and CI generation

7.1 **Regenerate-only.** Managed files (CI workflows, publish workflows, goreleaser, wrappers,
launcher shims) are machine-owned artifacts: regenerated wholesale, committed, chmod 444, never
hand-edited. **Deleted:** the entire three-way merge subsystem — stored bases, base-healing from
history, conflict markers, `managed-files.json`, orphan sweeps, scaffold-conflict checks, hash
pruning. Fleet evidence: across 27 repos there were exactly 11 hand-customizations of merged files,
every one of them CI-job setup; zero on publish workflows. [%%]

7.2 **CI customization = typed intent requirements** (never steps): projects declare needs —
`databases` (engine/version/extensions), `browsers`, `cli_tools`, `git_capabilities`,
`language_matrix`, `package_root`, `test_profile` — and the tool owns the step recipes that derive
all YAML. Unknown requirement = hard error until the tool ships a recipe (deliberate: the bottleneck
is the feature). The recently-landed `services` + `test_env` config keys and the `requires-services`
check are the donor vocabulary — the current implementation already started walking toward this
model. All 11 observed fleet customizations map onto the schema. [%%]

7.3 **No `ci-custom.yml` concept.** rlsbl owns its generated workflow filenames and ignores everything
else in `.github/workflows/`; hand-authored workflows are ordinary files needing no sanction. (OIDC
constraint stands: `publish.yml` keeps its filename — trusted publishing binds to it.) [%%]

7.4 **Lint configs are reclassified as user config** (create-once, never merged) — they were never
generated artifacts in nature; they likely migrate to the analysis tool (see 9) eventually.

7.5 **Template engine is an implementation detail** of the recipe emitters, not a design decision;
the only requirement is canonical deterministic output. [%%]

7.6 `.gitignore` keeps its additive set-union model (not three-way, unaffected by 7.1).

## 8. Repo surgery and command surface

8.1 **Surgery = three commands:** `extract` (operates on releasables — the portable unit;
`extract-releasable` and package-level extract collapse into it), `absorb`, and `mirror`, the latter
two ported with their fresh redesigns (absorb: round-trip-coherent history rewrite under the
destination prefix with tag import and changelog-hash remap; mirror: plan/apply reconciler of a
tool-owned derived artifact with foreign-commit tripwire). Package-level extraction from a shared
releasable is deliberately manual: split the releasable inside the workspace first (human judgment
over changelog attribution and starting version), then extract mechanically. [%%]

8.2 **Watch = core sync only:** poll runs for a SHA, failure classification with log tails, one
bounded auto-retry via in-place rerun, desktop notification, publish-workflow audit. **Deleted:**
`--watch-async`, detached watchers, pidfiles, `watch --stop` — rlsbl exits process supervision;
callers wanting non-blocking watch use their own background primitives. [%%]

8.3 **Structured output on every read command:** `--json` with a schema-defined, versioned shape on
all reporting commands (status, check, unreleased, targets, graph, impact, watch results, …).
Mutating commands emit human text + stable exit codes; their `--dry-run` output is schema'd JSON on
request. [%%]

8.4 `check-name` / `claim-name` are kept. `discover` is kept. **[OPEN]:** `prs`, `record-gif` (might
be useful if rlsbl is ever used by teams; genuinely undecided — do not resolve unilaterally).

8.5 **Blog integration = the `blog-publish` typed pipeline kind** (5.1); the release-file `blog` field
and body-file flow are its inputs. [%%]

## 9. Source-code analysis leaves rlsbl

9.1 **Invariant: rlsbl never parses source code.** The entire tree-sitter subsystem — import scanning,
`deps-unused`/`deps-undeclared`/`deps-runtime-test-only`/`deps-dev-in-lib`, `dead-modules` (+
staleness), `dead-workspace-packages`, `circular-deps`, `library-lint`, `unreachable-code`, all four
grammars, both backends — moves to **strictcode**, consumed via the structured external-check
protocol (like pgdesign today). The transfer document (seed catalog, per-language import semantics,
test-context rules, config surfaces, 32-lesson regression register) has already been absorbed into
strictcode's DESIGN.md. rlsbl keeps only declared-metadata checks (layers, deps-stale, version
consistency, workspace structure). Timing: the gap between removal here and strictcode shipping is
accepted (checks go dark in the interim; the boundary is permanent, the gap is temporary).

9.2 **Casualties with no successor anywhere** (deliberate): `dunder-version-missing` (strictcode ruled
it version-management, not architecture); **AST-based `__version__` bumping during release** (pypi
packages should single-source their version / use `importlib.metadata`; fleet sweep item at
migration); built-in Maven/JVM lint delegation (a project that wants detekt/checkstyle declares it as
an ordinary external check); Dart source analysis (no consumers); regex lint fallback backends.

## 10. Policies

10.1 **Env vars: config-only inputs.** All five `RLSBL_*` input-override env vars die
(`PUSH_TIMEOUT`, `CHECK_TIMEOUT`, `HOOK_TIMEOUT`, `BUILD_TIMEOUT`, `BUILD_TIMEOUT_{TARGET}`); config
keys are the single source; the hook-timeout budget becomes typed per-entry `timeout` fields on
checks/pipelines. A `--push-timeout` CLI flag covers the one real one-off workflow found in the fleet
(slow monorepo push at 300s). Exported context becomes structured fields / CLI args composed by rlsbl
for typed consumers; env interpolation survives only for operator-authored shell-string contracts
(`RLSBL_DIST_DIR`-style). The three handshakes (`RLSBL_SCRUB_ORCHESTRATED` — read by safegit;
`RLSBL_RELEASE_PUSH`; `RLSBL_PUSH_STDIN`) survive and should be formally declared via strictcli's
handshake-env primitive rather than raw getenv. [%%]

10.2 **Git hooks: committed, via `core.hooksPath`.** Scaffold generates hook files (pre-push,
post-rewrite) into a committed regenerate-only directory and sets `core.hooksPath` (one git-config
write at scaffold time, verified by a check that names the fix for fresh clones). The content-hash
hook-management machinery (`PRE_PUSH_HOOK_HASHES`, known-content overwrite heuristics) **dies**; hook
updates become ordinary generated-file commits. Hooks exec the binary named by `released_by` (a name
resolved via PATH — committed files stay machine-portable; never absolute paths, never versions).

10.3 **No migration surfaces in the tool, ever.** One-time transformations are throwaway scripts that
die when their transition completes (precedent: the migrate command that errored on every invocation
from the day it shipped until it was deleted, because one-time tooling has no ongoing users to notice).
Breaking state changes ride strictspec migrations (3.2); fleet sweeps are scripts, not commands.

10.4 **Everything in the codebase must justify itself as permanent.** No transition machinery, no
"meanwhile" shims, no interim detection for states that exist only until the next work item lands.

## 11. Target scope

11.1 **Keep** (fleet-used): pypi, npm, go (binary + library, with companion tags), docker, zig, swift,
maven, spec, plain, pgdesign, and the cloudflare-pages docs pipeline. zig/swift/maven were explicitly
confirmed despite light usage — full fleet coverage, no orphaned projects. [%%]

11.2 **Drop** (zero fleet usage): cargo, deno, hex, dart, flutter (including the entire OTA/Shorebird
apparatus: mode field, native-file detection, build-number management), native-android, native-ios,
swift-apple. Re-adding a target later is additive.

## 12. Fleet migration notes (case-by-case work discovered in the surveys)

- Add residual-claimant root packages to the 5 workspaces lacking one; choose releasable-assigned vs
  dev_only per repo.
- Register or claim every orphaned-code path found in the uncovered-path survey (unregistered CLI
  package, native app code, benchmark harness, prototypes, IaC dirs).
- Convert the three implicit-mode workspaces to explicit single-package releasables.
- One workspace runs three releasables in a shared bare-`v*` tag namespace — tag attribution is
  ambiguous there today; migration must introduce prefixed tags going forward with boundary handling.
- One workspace carries a tag prefix derived from a member name rather than its releasable name —
  carried forward as explicit per-releasable `tag_format` data.
- One workspace still has an orphaned root `.rlsbl/` (already failing the root-marker-conflict check).
- Resurrect the destroyed production deploy config as the first `ssh-deploy` pipeline (5.4).
- `__version__` single-sourcing sweep across pypi packages (9.2).
- Four orphaned base-less `ci.yml` strays (left behind by the single→multi-target rename) get deleted
  when 7.1 lands.

## 13. Open items ([OPEN] — ask before resolving)

- Deploy-elevation framing (5.4) — direction agreed in discussion, final confirmation pending.
- `preid` / pre-release channels (4.5).
- `prs`, `record-gif` (8.4).
- Any rename for `dev_only` (1.7) — only with a genuinely better candidate.

## Effort

This is a multi-phase program, not a task. Rough dependency order for adopting in the current
implementation: strictspec must exist before 3.2/3.3 (everything else is independent of it);
1.x/2.x (model + partition) are one coordinated breaking change with a fleet sweep; 4.x (engine),
6.x (changelog), 7.x (scaffold), 5.x (lifecycle seams) are each independently adoptable breaking
changes; 8.x/9.x/10.x/11.x are mostly deletions plus small features. Every phase that changes state
formats ships with its sweep. Release once per coherent phase, not per item.
