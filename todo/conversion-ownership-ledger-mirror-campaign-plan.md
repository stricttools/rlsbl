# Campaign plan: conversions, workspace ownership, release ledger, publication reconciler, release mirrors, target support matrix

Third-generation implementation plan for a single campaign across rlsbl.
Every design decision is made and ratified by the user; no phase contains
open design work. The plan was grounded against the working tree and has
survived two adversarial critique rounds (veracity, consistency,
duplication, practice), with every finding either fixed here or ruled on
by the user.

Campaign lifecycle (stated once; phases refer back here):

- FIRST, before the campaign proper: a small pre-campaign patch release
  of rlsbl ships the consolidation fixes (phase P below), so the version
  that deferred workspaces pin cannot corrupt their history. This is a
  separate natural release preceding the campaign, permitted by the
  published-artifact exception to release-once.
- Then the machine-wide editable install is replaced with a normal
  install of that patch release (standing rule in ~/Projects/CLAUDE.md,
  "Pin the install before campaigns on fleet tools"). Every repo on the
  machine runs released rlsbl while the campaign proceeds.
- One campaign release ships everything else, at the very end.
  Intermediate phases accumulate as local commits on main.
- After the campaign release: the fleet workspace sweep runs FIRST (the
  migration script executes from the checkout via uv run, needing no
  installed rlsbl), THEN the editable install is restored — so no fleet
  repo ever observes the upgrade errors, except the workspaces whose
  conversion is deliberately deferred to their own todos (they pin the
  patch release and convert on their own schedule).

## Terminology

- "Member" and "project" both mean a workspace entry in workspace.toml
  (the codebase says project; this plan says member). "Package" in
  changelog-entry context means the member a change is attributed to.
- "The ledger" is not a new artifact: it is the set of archived release
  files (TOML, under the releases state directory per project or
  releasable) once they carry the anchor fields. No separate store.
- A command's "preview" is its printed would-do assessment (the plan
  half of plan/apply). "The campaign plan" is this document.
- "The publication reconciler" is the extended `release reconcile`
  command (phase 8). "The mirror reconciler" is `monorepo mirror`.
  The word tripwire is always qualified with one of those.
- The reserved root identity: every workspace's path-"." member carries
  the literal name `root`. Its workspace-file entry is "the root member
  entry".

## Decisions this plan encodes

Decision origin: the rulings below are the user's deliberate decisions
except the specific elements listed at the end of this section, which
were adopted on trust of the session's recommendation (freely
reversible, never to be cited as deliberate intent).

Conversions:

- The releasable is the portable unit. Extraction operates on
  releasables only; absorb always targets a releasable, auto-creating a
  singleton for a bare package.
- Implicit workspace mode is DELETED, including the migrate-releasable
  command and module (no functions move anywhere — the deferred
  workspaces convert themselves on the pinned patch release, per their
  own filed todos; the campaign converts nothing). Post-campaign, an
  implicit-mode workspace is a load-time hard error naming the pinned
  patch release and the filed-todo path as the remedy.
- Extract is a verified move-out: the source copy is deleted only after
  tree-object identity is proven — per member, the source subtree's
  tree hash equals the corresponding tree in the new repo, taken after
  the history filter and before any state transforms. Deletion goes
  through saferm; when saferm is absent, rm runs only under the
  `--delete-with-rm` flag (presence-optional; saferm absent without it
  is a hard error naming both remedies). Extract is consequential.
- Conversion consent: plan/apply within one invocation — the preview is
  computed and shown, apply proceeds under `--approve-consequential`.
  The file-driven consent contract belongs to the publication
  reconciler only.
- Absorb runs its rewrite in a working clone under the monorepo's own
  .git/rlsbl/ directory (announced in the preview), merges by fetch,
  and heals crashes by idempotent re-run (completed steps detected: the
  merge by trailer plus source identity, tags by presence at mapped
  commits, the workspace entry by content).
- Imported historical tags take the destination releasable's tag format
  uniformly plus exactly one boundary alias at the current version;
  ref-name collisions and same-version-on-both-sides are preview-time
  hard errors. The singleton releasable's tag format is written
  explicitly, derived from the member's primary target scheme; targets
  spanning both schemes are a preview-time hard error naming an
  operator-declared tag format as the remedy.
- Conversions record facts (predecessor/successor identity, old-to-new
  tag maps including anchor remaps, departed tag globs, boundary
  aliases, promotion split maps) in a committed lineage record in the
  releasable state directory — in a standalone successor repo, under
  its own .rlsbl state directory. Follow-up obligations are standing
  checks from facts plus reality; the PyPI Trusted Publisher residual
  is discovered at first publish and recovered via release retry, and
  BOTH conversion directions print the exact settings path when a PyPI
  target moved.
- Sibling manifests are never mutated by extract: inbound
  local-dependency edges hard-refuse, the preview naming the exact
  rewrite-command invocation per edge (registry floor at the locked
  version; a never-published floor is its own hard error); the
  departing name joins internal_dep_floors. Registry dependencies
  between siblings are never auto-converted on absorb (pinned by test).
- Two standing hard errors in dev sync and dev status: an editable
  overlay whose package is a member of the current workspace, and an
  overlay whose path resolves inside the repo.
- Manifest rewriting lives in a new `rewrite` command group:
  `rlsbl rewrite go-module-path`, `rlsbl rewrite uv-path-sources`.
- rlsbl never acts on external systems it does not own (repository
  administration, registry configuration, releases in other repos);
  completion output prints next-step commands. (It does own and write
  GitHub Releases and tags for its managed repos, per the namespace
  ownership statement.)

Ledger:

- Every archived release file records the released commit (the
  CI-verified candidate SHA) and the tree hash of the released path
  (the member's subtree for workspace members; the root tree for
  standalone projects), authored by the finalization step before the
  file is locked. An anchor present in the pre-release editable file at
  release validation is a hard error (anchors are release-authored
  only); release undo STRIPS anchors when restoring the file.
- The released-commit authority chain: the ledger anchor is the
  authority; the marker embedded in GitHub Release bodies (which CI's
  publish workflow parses — it cannot read the ledger, because the
  tag's checkout predates the finalization commit) is the anchor's
  projection; ONE shared Release-writing helper (release flow,
  publication reconciler, mirror publication) emits notes, marker, and
  prerelease flag from the ledger.
- Read semantics map by QUESTION, not by command: every unreleased
  range and coverage computation selects the highest anchored version
  whose commit is an ancestor of the checkout; every latest-release
  FACT display uses the absolute highest, visibly annotated when the
  checkout predates it; preparing a release on a checkout lacking the
  latest anchored release is a hard error (divergent line). Three read
  errors, each at its site: DISAGREEMENT (tag exists, points elsewhere
  than the anchor), INDETERMINABLE (needed commit absent from
  truncated history — shallowness itself is fine; the error names the
  deepen remedy), MISSING-ANCHOR (pre-migration archive; the error
  prints the complete single-version recovery — tag-derived value,
  unlock/edit/relock — the one place rlsbl instructs manual state
  editing, re-validated by the next check run).
- Anchor backfill is a repo script (backfill precedent), dry-run first,
  idempotent, one-time and pre-reconciler; it also runs per-repo during
  the fleet sweep. Descriptions for description-less historical
  versions are authored meaningfully by subagents from each version's
  own changelog entries and commits; the tagless version's commit is
  recovered by history inspection and anchored (the reconciler later
  materializes its tag); an explicit unanchorable marker only if
  recovery fails.
- Ancestry checks gain an INDETERMINABLE outcome; it routes to each
  caller's fail-closed branch, except where the false-branch is itself
  the conservative safe action (the changelog cache check recomputes).

Publication reconciler and checks:

- Tags and GitHub Releases are a tool-owned derived namespace converged
  by the publication reconciler: the existing `release reconcile`
  extended with the ledger plus lineage records plus the committed
  scrub archives as explanation sources beside the rewrite journal —
  one command, one merged preview of per-version verdicts (materialize,
  already-correct, re-point-with-lease for wrong-commit,
  refuse-foreign, refuse-identity-mismatch), leased writes, and a
  tripwire that hard-errors on any ref no source explains, touching
  nothing. Consent is file-driven: preview writes a file, apply reads
  it, the command stays consequential.
- expected_refs(version, context) on the target protocol is the single
  authority for a version's full ref set (primary, companions, and
  RECORDED aliases from lineage/rename records). It absorbs the
  companion-tag collector's rules (publish-mode skips, path-scheme
  suppression); the release tag step consumes it; the three shipped
  warn-severity tag/Release checks are DELETED in the same change as
  superseded surfaces. The unpublished-refs standing check renders the
  same authority with three distinct errors, every error naming
  `release reconcile` as the remedy.
- Per-target materialization policy is a protocol axis (default
  materialize; Go refuses recorded-module-path mismatches); the
  published-evidence guard gains a Go module-proxy evidence source,
  both Go sources combining under the existing fail-closed rule.
- Network-dependent checks are fail-closed: inconclusive is a hard
  error.
- Namespace ownership (the never-push restatement, written once, in
  phase 9 where the writer set completes): origin branch heads —
  releases only; origin tags and GitHub Releases — the release tag
  step and the publication reconciler, both through the shared Release
  helper where a Release is written; the mirror branch — the mirror
  reconciler's converge; mirror tags and Releases — the shared mirror
  publication module; the history-scrub force-push remains the one
  sanctioned rewrite write.

Mirrors:

- Mirror destination is a releasable-level field (multi-member
  releasable with a mirror is a hard error; the old per-project key
  becomes a loader error in the same change that adds the
  replacement). The release's mirror step invokes the mirror
  reconciler's converge (leased) and publishes the converged tip's tag
  and Release through the shared mirror publication module; the old
  unforced main-branch push is deleted. A mirror-step failure is
  NON-FATAL: recorded, the release completes, the exit status names
  `release reconcile` and the mirror reconciler as healers (aborting
  would un-do nothing — origin is already published).
- The mirror scaffold commit rewrites go.mod to the mirror identity;
  go.mod joins the mirror's scaffold-owned path set (widening the
  mirror tripwire's tolerance — deliberate; the other owned manifests
  deliberately get no identity rewrite, since their registry identities
  are repo-independent); mirror scaffolds drop the publish workflow. A
  Swift member requires a configured mirror (registration-time check).
  Both mirror-related target behaviors are protocol axes, keeping the
  target-name-literal guard green.
- Mirror promotion (extracting a mirrored releasable adopts the
  mirror's public history): the deletion proof is tree-hash equality
  between the monorepo subtree and the mirror's pre-scaffold split
  commit (subtree split preserves trees); the split correspondence map
  is persisted into the lineage record because it must travel with the
  extracted repo.

Workspace ownership:

- Every non-tool-owned file has exactly one owning member: most
  specific member path wins; the mandatory root member (reserved name
  `root`) owns the remainder minus the tool-owned exempt set, which is
  derived from STATIC path rules only. This exempt set and the mirror
  tripwire's per-repo allowlist are deliberately distinct predicates
  answering different questions (time-stable attribution vs
  scaffold-write tolerance on one mirror); they are named apart and
  must never be unified.
- The watch key is deleted. Load-time hard errors (loader-sited; safe
  because migrations are scripts editing raw files, and the pinned
  install covers the campaign window): no root member; a root member
  named anything but `root`; a non-root member named `root`; a watch
  key; an implicit-mode workspace; a root-member releasable without an
  explicit tag_format. The relocated mirror-remote key errors in phase
  9 with its replacement. Errors whose remedy is mechanical name the
  migration script; the two requiring operator decisions (the name
  collision, the tag format) state the decision instead.
- CI triggering derives from declared dependency edges (all scopes)
  plus built-in rules (workspace-root manifests/lockfiles trigger all;
  release-machinery auto-append; router change reruns all); filters
  gain negated excludes; the generator and the release-time simulation
  share one negation-aware matcher, validated against a committed
  fixture of the real CI filter library's verdicts (captured once by a
  hand-run Node script, keyed to the pinned filter-action version,
  regeneration demanded by a check when the pin changes).
- Root-directory resolution: the root member resolves like any member
  for project-scoped commands (resolution can no longer return
  nothing); workspace-detection call sites are reclassified to detect
  the workspace explicitly. `release run` at any workspace root
  requires `--releasable <name>`; absent is a hard error listing the
  releasables.
- `monorepo init` REQUIRES the root member's kind to be declared (dev
  node, or member of a named releasable); absence is a hard error
  explaining both. No scaffold default.
- Auto-derived packages lists on changelog entries narrow to
  single-owner attribution; manual broadening via changelog edit
  remains.
- A commit whose changed files cannot be determined is a hard error.
- Root members without a detectable root manifest are exempt from the
  stale-entries and targets checks (new machinery for stale-entries,
  which today has no such filtering; copied filtering for targets).

Target support:

- All targets stay; focus on the main ones. Per-axis migration follows
  one deterministic rule — behavior invoked per-target becomes a
  protocol method; aggregate sets consumed elsewhere become
  registry-derived structures — applied in an explicit table (phase
  2.1); the declared detection-file list feeds the detection method's
  default, content-inspecting targets keeping overrides. Every feature
  declares one explicit unsupported-target policy; the test runner's
  unknown-target silent success becomes a skip naming the target,
  surfaced in the release step summary (candidate CI remains the net).
  The capabilities attribute is deleted in favor of introspection.
- The committed matrix artifact forms one derivation chain: target
  classes (authority) → one generator → the committed matrix file →
  the docs directives READ THE FILE (re-pointed from live
  introspection; this also removes the import-failure class in docs
  builds and makes versioned docs historically accurate).
- pgdesign's schema-subdirectory detection fallback is deleted; the
  hard error fires at use (version/schema resolution), never in
  detection. General error-siting convention below.
- The four-place check registration convention stands for this
  campaign (reduction filed: todo/single-declaration-check-registration.md).

Trust-adopted elements ([%%] class): extract's consequential
classification; the packages-narrowing; the retired-key load-time
errors; the singleton tag-format derivation and mixed-scheme refusal;
the root-member check exemptions; the in-file anchors on the
CI-verified candidate; the in-place backfill of read-only archives; the
file-driven reconciler consent; the mechanics bundle (Go proxy evidence
source, repo lock on conversions, reserved-name rules, reused job-key
derivation, persisted promotion split map).

## Phase dependency table

| Phase | Depends on | Why |
|---|---|---|
| P | — | pre-campaign patch release |
| 0 | P | pins the install to P's release |
| 1 | 0 | fixtures, preview hygiene |
| 2 | 0 | fixtures |
| 3 | 0 | fixtures, skeleton unused but fixture sweep needed |
| 4 | 0, 2, 3 | expected_refs is a protocol axis (2); shared files edited after 3 (assignment below) |
| 5 | 0 | standalone commands; obligations: effects classification, schema dump, pinning tests |
| 6 | 0–5 | rebuild consumes skeleton, checks, ownership, ledger, rewrites |
| 7 | 0 (lineage), 6 (fact writers) | conversion-fact checks need writers; two checks are purely local |
| 8 | 0 (skeleton), 2 (policy axis), 4, 6 (lineage) | merged planner over ledger + lineage + scrub archives + journal |
| 9 | 3 (workspace schema), 4, 5 (Go rewrite), 6 (extract), 8 (Release helper, verdict shapes) | |
| 10 | everything | |

Shared-file assignment (referenced by the table): where phases 3 and 4
touch one function, attribution threading (the full-member-list
rewrite) is phase 3's edit and version/range anchor reads are phase
4's; phase 3 lands first in every such file. Phases 4, 8, and 9 each
add a protocol axis and therefore each carry a matrix-regeneration
bullet (the phase-2 completeness assertion and freshness check demand
it).

## Executor conventions

- Red-green for every defect fix.
- One commit per item via safegit; machine-generated files via rlsbl
  commit. Never push; the campaign releases per the lifecycle above.
- Full suite green at every phase boundary, not only at the end —
  candidate CI runs exactly once, so local discipline is the net.
- Every new check registers four-place (implementation, checks
  metadata registry, check-to-target matrix, docs table row).
- Every new command updates the schema dump, regenerated docs, and the
  wiring/effects/consequential pinning tests.
- Error siting: a new hard error fires where the feature is used,
  never inside detection or parsing that runs everywhere.
- Repo-wide sweeps: dry-run, expected-occurrence assertion, full diff
  review; enumeration output committed with the change.
- Subprocess helpers take explicit timeouts (the conversion module's
  git helper currently takes none — fix on touch).
- One verify bullet per goal bullet, everywhere.
- Before the release: review the log for commits from other sessions
  (standard shared-worktree protocol).

---

## Phase P — Pre-campaign patch release

Effort: small.

- Fix, red-green, the two history-corrupting consolidation defects in
  the releasable migration module: entry identifiers dropped through
  consolidation/merge/dedup, and the truncation of a releasable's
  unreleased file when no member has entries.
- Release as a normal patch (standard release flow). The deferred
  implicit-mode workspaces' filed todos pin this release.
- Verify: red-green tests for both; the release ships; the todos'
  described pin ("at or after the consolidation-fix patch") is now
  satisfiable.

## Phase 0 — Foundational groundwork

Effort: medium.

### 0.0 Pin the install

- Replace the editable install with a normal install of phase P's
  release. Record the exact restore command in a committed file
  alongside this plan. The fleet workspace list is NOT committed (the
  repo is public; other projects' paths stay out) — the sweep derives
  it at run time by scanning for workspace markers, or reads a
  local-only file (gitignored fleet-wide by suffix).
- Verify: the version report shows the patch release; the editable
  path is off the import path; the committed file exists and contains
  no foreign project paths.

### 0.1 Conversion and root-member test fixtures

- Extend the conversion test helpers (which build the outdated
  per-package layout) to build explicit-mode workspaces with releasable
  state directories, reusing the existing multi-releasable fixture
  factory; add releasables and root-member support to the shared
  make_workspace helper (it already accepts path-"." members).
- Verify: at least one existing conversion test re-expressed on the
  new fixtures passes; fixtures exercised by the suite.

### 0.2 Preview hygiene for conversions

- Fix the one bare `git status` in absorb's precondition check to the
  allowlisted no-optional-locks form. This precedes the phase 6
  rebuild deliberately: developing the rebuilt absorb's preview
  requires a working CLI-level dry run, and the durable deliverable is
  the regression test, not the one-line fix.
- CLI-level regression test driving absorb's dry run through the app
  with a bound effects context.
- Sweep the conversion modules for other non-allowlisted observations;
  commit the enumeration.
- Verify: the CLI-level dry-run test passes; allowlist tests green;
  enumeration committed.

### 0.3 Shared preview/apply skeleton

- Lift the shared shape from the mirror command into a command-neutral
  module: a preview is an ORDERED LIST OF KEYED VERDICT ITEMS (each:
  subject key, state, observed facts, would-do action); one renderer
  prints any preview; the observe/branch/apply entry skeleton with the
  no-writes-above-this-line convention. The mirror's whole-repo state
  becomes the one-item case. All writes through the effects
  chokepoint; the module lives outside the commands package,
  importable by monorepo and release commands. Consumers: mirror
  (migrated now, behavior-identical), the rebuilt conversions (phase
  6), the publication reconciler (phase 8) — both later phases name
  this module.
- Reconcile ALL ancestry-check implementations (the git-util helper,
  the mirror's copy, the private one in changelog validation, and the
  inline one on the release candidate path) into one with an explicit
  INDETERMINABLE outcome, mapped per the ruled rule: fail-closed
  branch everywhere, except the changelog cache check where
  recomputation is the safe branch.
- Verify: the entire mirror test module passes unchanged; one shared
  ancestry helper with true/false/indeterminable tests; each caller's
  mapping has a test.

### 0.4 Lineage fact records

- The committed lineage record: append-style JSON in the releasable
  state directory (in a standalone successor repo: under its own
  .rlsbl state directory), recording conversion events, tag maps,
  ANCHOR REMAPS, departed globs, boundary aliases, identity
  transitions, and promotion split maps; written via the undo-audit
  append pattern; validated like other rlsbl documents. Name the
  actual reading code paths for any new config keys (the config schema
  validator only bans specific keys; there is no unknown-key registry
  to extend).
- Verify: unit tests for write, append, read, validation; malformed
  record is a hard error; the standalone-repo location round-trips.

## Phase 1 — Check-layer fixes

Effort: small.

- workspace-unregistered becomes target-path-aware: a directory that
  is a declared target path of a registered member or releasable is
  exempt.
- Delete the pgdesign schema-subdirectory detection fallback; the hard
  error naming the explicit-path remedy fires in version/schema
  resolution, never in detection.
- Verify: red-green for both; a repo with an undeclared schema
  subdirectory and no pgdesign target declared detects nothing and
  errors nowhere.

## Phase 2 — Target support matrix

Effort: extra large.

### 2.1 Protocol migration of behavior-encoded axes

- The enumeration sweep first: every target-name literal in a
  feature-support conditional; the committed sweep output is the work
  list.
- Apply the ruled assignment rule (per-target behavior → protocol
  method; aggregate sets → registry-derived) via an explicit table
  covering at least: yank dispatch (method), test runner (method; the
  unknown-target silent success becomes the surfaced skip), name
  registries and normalization (derived), check skip-sets (derived),
  the lint dispatch family (methods/derived — the same silent-skip
  shape on a parallel language taxonomy), the companion-tag check
  (calls the protocol method), and the detection duplication (declared
  detection files feed the detection method's default; the
  content-inspecting targets keep overrides).
- The plain target's other-manifests list derives from the registry
  plus a declared extras set for the manifests belonging to no current
  target, preserving detection behavior.
- The structural guard: a test that target-name literals in feature
  conditionals appear only in an allowlisted module set (the targets
  package plus the check-to-target matrix).
- Verify: sweep list fully ticked; behavior identical for supported
  targets, explicit skip/error for unsupported; the guard passes; a
  plain-target fixture with an extras-set manifest still refuses
  detection; the skip line appears in the release step summary
  fixture.

### 2.2 Capability derivation

- Delete the capabilities attribute; derive per axis by introspection.
  Enumerate ALL readers first; the attribute-with-default read pattern
  is banned in the replacement (two probe-deciding sites would
  otherwise silently degrade).
- Correct the false docs claims (the nonexistent capabilities in the
  pipelines doc; the name-consistency sentence in the targets doc).
- Verify: derivation tests replace the old validity test; the two
  probe-deciding sites provably consult the derivation.

### 2.3 The committed matrix artifact

- One generator in the targets introspection module produces the
  committed matrix file covering every axis; committed via rlsbl
  commit; freshness check (regenerate-and-compare) registered
  four-place; import-time completeness both ways (new target must
  answer every axis; new axis must cover every target).
- Verify: freshness red on unregenerated change, green after; the
  completeness assertion fires on a synthetic incomplete target.

### 2.4 Docs derivation

- Re-point the existing docs directives to READ the matrix file
  (removing their live-import path — also removing that failure class
  and making versioned docs historically accurate); delete the
  hand-written duplicate tables; switch hand-typed counts to derived
  counts; remaining prose counts-free.
- Verify: docs build; deleted-table markers gone; a directive test
  reads the file without importing the registry.

## Phase 3 — Workspace ownership model

Effort: extra large. The root-attribution defect, the watch deletion,
and the fixture migration are ONE atomic cluster (3.1+3.2): a root
member currently matches nothing by prefix, watch is every existing
root member's only territory, and the entire test fixture corpus is
implicit-mode and root-less — the loader errors and the fixture sweep
must land together or the suite is red for the rest of the phase.

### 3.1 Single-owner attribution core

- One ownership resolver: full member list + file path → single owner
  (most specific path wins; root residual; the 3.7 exempt set
  excluded; invariant: every non-tool-owned file has exactly one
  owner). Fix the root path-prefix computation. 3.7's function is
  built first within this phase; order the subphases accordingly.
- Rewrite the git-util attribution functions around it; thread the
  full member list through every caller (changelog validation scope,
  the checks common context and its consumers, prepush, changelog
  add's scope check and packages derivation — implementing the ruled
  packages-narrowing — status, unreleased, monorepo status coverage,
  batch init, the workspace test-suite check). Collapse impact's
  duplicate longest-prefix mapper and the cwd resolver's own
  longest-prefix rule onto the resolver.
- Unattributable commit = hard error naming commit and operation.
- Verify: property tests (exactly one owner per non-tool-owned file;
  most-specific wins; root residual; exemption matches 3.7's
  function); the narrowing has its own test (a spurious multi-claim
  from the old model no longer appears; manual broadening via
  changelog edit still works); unattributable fixture errors; the
  consumer suites pass.

### 3.2 Root member, reserved identity, loader errors, fixture sweep

- The single loader-error enumeration (the authoritative list, also
  consumed by the phase 10 script's scope): no root member; root
  member misnamed; non-root member named `root`; watch key present;
  implicit mode; root-releasable tag_format missing (folded here from
  the tag-format subphase). Mechanical-remedy errors name the
  migration script; the two operator-decision errors (name collision,
  tag format) state the decision.
- `monorepo init` requires the root-member kind declaration (dev node
  or named releasable member); absence is a hard error explaining
  both; init scaffolds explicit mode with an empty releasables
  section.
- Root members without a root manifest: exempt from the targets check
  (copied filtering) and the stale-entries check (NEW filtering — that
  check currently has none).
- Job/filter/check-regex derivation for the reserved name adopts the
  existing root-publisher derivation as the one convention.
- THE FIXTURE SWEEP, in the same subphase: centralize the shared
  workspace fixture helper (most call sites go green in one place) and
  sweep the hand-written workspace-file fixtures across the test tree
  onto the new model; enumerated, count-asserted, diff-reviewed.
- Regenerate the committed router here (accepted as provisional until
  3.5's freshness check exists).
- Verify: loader fixtures for every enumerated error with message
  content asserted; init fixtures (declared kinds, absence error);
  both exemptions tested; derivation produces valid workflow keys; the
  suite is green at the end of this subphase.

### 3.3 Root-directory command resolution

- Enumerate ALL cwd-resolution call sites by grep and classify:
  project-scoped (resolves to the root member) vs workspace-scoped
  (explicit workspace detection). State the contract change:
  resolution inside a workspace can no longer return nothing.
- `release run` at any workspace root requires `--releasable <name>`;
  absent errors listing the releasables; member-directory and plain
  standalone invocations unchanged.
- Verify: per-site tests; release-run fixtures at single- and
  multi-releasable roots (both demand the selector; with it, both
  release the named releasable); check at the root iterates every
  releasable.

### 3.4 watch removal residue

- Remove the key from types, the add command and flag, list output,
  router generation; rewrite every user-facing text naming it
  (changelog scope errors, CI remediation, release empty-window
  remediation, docstrings feeding generated docs, non-historical doc
  pages).
- Verify: a grep-style pin scoped to the workspace-key read surface
  (the property accessor, dict reads of the key, the add flag) — not
  the word itself, which legitimately appears as a release flag, a
  command name, and in the migration script and loader error.

### 3.5 Trigger derivation and router filters

- Filters derive from ownership + dependency territories (all scopes;
  sync gains a workspace-graph construction whose failure modes
  surface as command errors) + built-ins (workspace-root
  manifests/lockfiles trigger all; machinery auto-append generalizing
  the finalize-artifact append; router change reruns all) + negated
  excludes; the root member's pattern is match-everything plus
  excludes.
- One negation-aware matcher shared by generator and release-time
  simulation; validated against the committed verdict fixture captured
  from the real filter library (hand-run Node script, keyed to the
  pinned action version; a check demands regeneration when the pin
  changes). Four-place registration for that check and the new
  router-filters freshness check (regenerate-and-compare — nothing
  polices the filters block today).
- Verify: generator and simulator agree on the fixture including
  negation ordering; a derived-filter observation test (a fixture
  workspace's generated filters match expectation); the freshness
  check reds on a stale block; batch-release contract tests pass;
  fixture-to-pin linkage test.

### 3.6 Root releasable tag format mechanics

- The tag-format field becomes explicit-or-absent (sentinel); the
  loader error itself is enumerated in 3.2; round-trip preserves
  explicit values. The reference sweep across source and tests is
  enumerated and count-asserted.
- Audit parsed-scheme consumers for bare-version tags inside a
  workspace (status, coverage anchoring, rename, mixed-scheme guard
  with a Go root member).
- Verify: round-trip fixtures; a bare-version root releasable passes
  status and coverage; sweep count asserted; error message content
  asserted (matching 3.2's style).

### 3.7 Derived tool-owned exempt set

- One enumeration function from static path rules only (generalizing
  the changelog exemption rules); never per-repo mutable registries.
  State, once, the deliberate distinction from the mirror tripwire's
  per-repo allowlist (different questions; never unify).
- Verify: unit tests; the 3.1 property test consumes it;
  workspace-machinery commits remain coverage-exempt.

### 3.8 Remaining test overhaul

- Replace the two vacuous root-member coverage tests with positive AND
  negative assertions; rewrite the watch-pinning tests onto the new
  model; remove the inert conftest fixture that sets a timeout
  environment variable rlsbl no longer reads (fix on touch).
- Verify: an uncovered root-owned commit fails coverage; suite green
  on a quiescent tree.

## Phase 4 — Release ledger

Effort: extra large.

### 4.1 Anchored archives

- Optional anchor fields (CI-verified candidate SHA; tree hash of the
  released path — member subtree in workspaces, root tree standalone)
  in the release-file schema; validator regenerated; reader binds
  them. The anchor-in-editable-file hard error fires at release
  validation. Undo's unfinalize STRIPS anchors on restore.
- Finalization authors the anchor into the archive before locking (the
  standalone path currently renames verbatim — this is the one new
  authorship step); the batch synthesize path gains the fields
  directly. The authority statement: anchor authoritative; the
  Release-body marker is its projection (the helper that enforces this
  is built in phase 8; until then the release flow's existing marker
  write stands). Schema-header authority note updated; reader tests
  police the schema-to-code edge.
- Verify: schema freshness; released fixture carries both fields with
  the correct tree; undo restore strips them and re-release re-authors
  them; the editable-file anchor fixture errors at validation.

### 4.2 The backfill script and rlsbl's own history

- The script (backfill precedent): anchors from tags across all
  recognized schemes; unlock/edit/relock via the established writable
  pattern (rename the JSONL-named helper or add a TOML sibling);
  stamps the missing format-version marker on archives predating it;
  materializes missing archives from recoverable sources. Dry-run
  first, per-version reporting, idempotent, one-time and
  pre-reconciler. It also runs per-repo during the phase 10 sweep.
- rlsbl's own history workstream, split for parallelism: (a) the
  description authoring — subagents write meaningful descriptions for
  every archive-less description-era-predating version from that
  version's changelog entries and commits; (b) the anchor pass —
  including the tagless version (history-inspection recovery; the
  reconciler materializes its tag later; unanchorable marker only on
  failure) and the one recognizable-but-foreign tag (a fourth
  taxonomy bucket: recognized scheme, no matching version — resolved
  or explicitly recorded, since it sits upstream of the phase 8
  tripwire).
- Verify: fixtures for a marker-less archive, a missing archive, and
  an unrecognizable tag (anchor+marker, materialized archive, and the
  operator-input error list respectively; a recognized old-scheme tag
  anchors normally); second run changes nothing; after the
  workstream, the phase 4.4 check (once registered) passes on rlsbl's
  own repo including the foreign-tag disposition.

### 4.3 The consumer switch

- Consumers enumerated by grep over describe/tag-list/rev-list
  anchoring (the known set — the last-tag primitive and callers, the
  coverage-range chokepoint, undo's commit-walk and predecessor
  lookup, the tag-list dialects, the unscoped describe display,
  watch's commit labeling, the destroyed-tag guard, extract's
  tag-decision reads — is the floor). The reconcile command's local
  tag listing is explicitly the observe layer, not an authority.
- Implement the ruled per-QUESTION semantics: ranges and coverage =
  ancestry-selected; latest-release facts = absolute with a visible
  divergence annotation; release preparation on a line lacking the
  latest anchored release = hard error. The three read errors at
  their sites with their messages. The destroyed-tag guard reads the
  ledger; a regression test encodes its dry-run false-positive
  scenario (restated in the test).
- Verify: per-consumer fixtures for agree/disagree/indeterminable;
  the divergence hard error at release-prep and the annotated
  fact display each have fixtures; the guard's dry-run test passes.

### 4.4 expected_refs and the unpublished-refs check

- expected_refs(version, context) on the target protocol: primary,
  companions (absorbing the collector's publish-mode and path-scheme
  rules), and recorded aliases from lineage/rename records. The
  release tag step consumes it; the three shipped warn-severity
  tag/Release checks are deleted in this change; the collector is
  deleted with its rules absorbed. This is a protocol axis: matrix
  regenerated, freshness re-passes.
- The unpublished-refs standing check renders it: three distinct
  errors (missing-locally, missing-remotely, wrong-commit);
  inconclusive probe is an error; every error names release
  reconcile. Four-place registration.
- Verify: the release tag step's pushed set equals expected_refs on a
  Go companion fixture and a renamed-releasable alias fixture; the
  deleted checks are gone from registry and docs; four-way check
  fixtures; matrix regenerated.

## Phase 5 — Rewrite command group

Effort: medium. Obligations: effects classification, schema dump,
docs, and pinning tests for both commands, per the conventions.

### 5.1 rlsbl rewrite go-module-path

- New rewrite group. Preview/apply: rewrites the module line (writer
  is new) and sweeps import sites line-anchored via the tree-sitter
  scanner. The module-prefix match rule is consolidated into ONE
  shared helper — sweep ALL copies (the import scanner, the
  dependency validator, and the CLI-detection module, whose second
  inline copy lacks the separator and wrongly prefix-matches similarly
  named modules — red-green that fix); count asserted.
- Verify: fixture repo rewrites completely; count mismatch aborts;
  preview lists every file; all former callers use the shared helper;
  the separator bug's regression test.

### 5.2 rlsbl rewrite uv-path-sources

- Generalizes the pypi build-time rewriter to the working tree across
  project dependencies, optional dependencies, dependency groups, and
  tool-uv-sources; floors at locked versions; unpublished floor is a
  hard error naming the release-first remedy; updates
  internal_dep_floors.
- Verify: workspace-source and path-source fixtures convert;
  unpublished errors; dep-floors passes after conversion.

## Phase 6 — Conversion rebuild as preview/apply

Effort: extra large. Built on the 0.3 skeleton.

### 6.1 Extract: verified move-out on releasables

- One extract command on releasables. Preview: tag translations and
  deletions, collisions, the per-member tree verification, inbound
  dependency refusals with exact rewrite invocations, the lineage
  record, next-step hints (Trusted Publisher path when a PyPI target
  moves). Preview-time refusals: a MIRRORED releasable (until phase 9
  wires promotion, which replaces the refusal); the releasable
  containing the ROOT member (extracting it would leave the source
  workspace without a root member — permanently refused with the
  remedy of restructuring first); extraction from a shared releasable
  (the error points at the manual split procedure documented in the
  conversion chapter, 6.4).
- Apply: clean tree; repo lock; clone and filter; PER-MEMBER
  tree-object identity verification (a multi-member filter unions
  paths, so each member's source subtree is verified against its
  destination tree); full state transplant including archives; ANCHOR
  REMAPPING through the filter commit map, recorded in the lineage
  record; tag translation with boundary tag; destination synthesis:
  the new workspace's root member entry and the extracted releasable's
  explicit tag format are written so the new repo passes its own
  loader; then the source side — saferm deletion (rm only under
  --delete-with-rm), workspace edit committed, router re-synced,
  snapshot regenerated, departed globs recorded, departing name added
  to the floor list. Consolidate the several clean-tree probes onto
  the shared helper.
- Consequential; the old not-consequential code comment and pinning
  test are updated in this change.
- Verify (one per goal): round-trip coherence on releasable-layout
  fixtures INCLUDING anchors resolving post-conversion with tree
  hashes as cross-check; per-member verification failure aborts
  naming both hashes; the deletion-consent matrix; each preview-time
  refusal has a fixture; dependency refusal names the phase 5
  command; floor-list write, lineage contents, boundary tag, lock
  held, destination loader-pass, source workspace checks green, PyPI
  hint — each asserted.

### 6.2 Absorb: releasable-only with healing

- Absorb targets a releasable (auto-singleton; explicit derived tag
  format; mixed-scheme preview-time refusal with the operator-declared
  remedy). Validation parity with monorepo add; scaffold and sync in
  apply. Working clone under .git/rlsbl/, announced. Uniform tag
  import with both collision classes as preview-time errors plus the
  boundary alias; fetched bare tags never deleted from under
  pre-existing monorepo tags; remap report surfaced; archives migrated
  WITH ANCHORS REMAPPED through the commit map (recorded in lineage);
  versioned files written locked; the silent same-version merge skip
  becomes an error; idempotent healing with the three detection
  predicates; repo lock; snapshot regeneration; hints including the
  Trusted Publisher path.
- The two overlay hard errors in dev sync/status; the
  registry-dependency non-conversion test.
- Verify (one per goal): healing at all three kill points; both
  collision fixtures; mixed-scheme fixture; releasable-layout absorb
  migrates archives with resolving anchors and surfaces the remap
  report; singleton tag format asserted in the written file;
  add-parity validations each fixtured; clone announcement asserted;
  locked file modes asserted; overlay errors fire; non-conversion
  test; lock fixture; snapshot fresh; workspace checks green.

### 6.3 Implicit-mode deletion

- Delete the migrate-releasable command, module, and tests outright
  (no functions move — deferred conversions run on the phase P
  release per their todos). Delete implicit-mode support everywhere
  it branches: cleanup's refusal, coverage and changelog routing
  splits, batch release's implicit-mode release-file sections, the
  loader (already erroring per 3.2).
- Verify: a grep-style test that the branch points are gone; help
  counts and pinned command tables regenerate; the implicit-mode
  loader fixture's error names the pinned-release remedy.

### 6.4 Conversion docs

- The narrative conversion chapter (semantics, tag policy,
  verification, the follow-up model, and the MANUAL SPLIT PROCEDURE
  for shared releasables that 6.1's refusal points at); regenerated
  CLI docs; the stale generated API pages for excluded packages
  removed (enumerate by comparing generated pages against the
  generation configuration — the earlier review found many); delete
  the implicit-mode sections from the monorepo guide.
- Verify: docs build; enumerated stale pages gone; the split
  procedure exists at the URL/anchor the refusal cites; freshness
  tests pass.

## Phase 7 — Standing checks

Effort: large (each check four-place; three are networked).

Decisions bullet coverage: these checks implement the
standing-checks-from-facts ruling plus two standing invariants
(module identity, lock consistency) accepted in the same ruling.

- go-module-identity (local): go.mod module path equals origin
  identity plus member subdirectory.
- dep-locks (local): lockfiles resolve current manifests (sibling of
  dep-floors).
- npm-token-presence (networked, via the GitHub API; the existing GET
  allowlist pin covers the argv — verify, else add a pinned entry).
- old-repo-archived and go-deprecation-published (networked; from
  lineage facts plus the GitHub API and the module proxy; both proxy
  readers extend the existing registry client).
- Networked checks fail-closed and network-tagged; local checks are
  ordinary error checks.
- Verify: per check — pass and fail fixtures; inconclusive fixtures
  for the networked three only; four-place registration; remedies
  named in error text.

## Phase 8 — Publication reconciler

Effort: large.

- Extend release reconcile (already consequential) on the 0.3
  skeleton: one merged preview over the explanation-source list — the
  rewrite journal, the ledger, lineage records, and the COMMITTED
  SCRUB ARCHIVES (the journal lives under .git and does not survive a
  fresh clone; the archives do) — emitting per-version verdicts:
  materialize, already-correct, re-point-with-lease (the wrong-commit
  repair), refuse-foreign, refuse-identity-mismatch. The journal
  logic is rebuilt as one source; the tripwire errors on any ref no
  source explains; observe reuses the remote-ref snapshot plus one
  Release listing; expected refs from expected_refs.
- THE SHARED RELEASE-WRITING HELPER is built here: notes, the
  released-commit marker, and the prerelease flag emitted from the
  ledger; the release flow and the reconciler both adopt it (phase 9
  adds the mirror module as the third caller). Reconciler-recreated
  Releases regain the marker.
- File-driven consent: preview writes the plan file; apply reads it.
- The materialization-policy protocol axis (default materialize; Go
  refuses recorded-module-path mismatches); matrix regenerated. The
  published-evidence guard gains the Go module-proxy source; the
  fail-closed combination rule is tested (proxy lag alone can never
  clear a deletion).
- Verify (one per goal): verdict fixtures for all five classes
  including the leased re-point; the tripwire fixture; a journal-only
  fixture reproduces current behavior through the merged preview; a
  fresh-clone fixture explains a scrubbed ref via the committed
  archive; helper-emitted Releases carry the marker (release flow and
  reconciler fixtures); plan-file round-trip; the Go mismatch fixture
  never materializes; the evidence combination test; matrix
  regenerated.

## Phase 9 — Release mirrors

Effort: large.

- Mirror destination on the Releasable (loader, serialization; the
  old per-project key becomes a loader error in this same change;
  multi-member-with-mirror is a hard error).
- One mirror publication module using the phase 8 Release helper; the
  release's mirror step invokes converge (leased) then publishes the
  converged tip's tag and Release through it; the old unforced
  main-branch push is deleted; failure is non-fatal, recorded, the
  epilogue naming the healers; step markers keep the completeness
  contract (trivially-done markers on no-op paths).
- The scaffold commit's go.mod rewrite (phase 5 rewriter) and the
  Swift-requires-mirror check are PROTOCOL AXES (keeping the 2.1
  guard green); go.mod joins the scaffold-owned set (deliberate
  tripwire widening; the other owned manifests deliberately get no
  identity rewrite); mirror scaffolds drop the publish workflow.
  Matrix regenerated for the new axes.
- The mirror reconciler's preview gains a tags dimension (missing
  release tags materialize from the ledger through the module).
- Promotion: replaces 6.1's mirrored-releasable refusal; adopts the
  mirror history; deletion proof = tree equality against the
  pre-scaffold split commit; the split map is persisted into the
  lineage record (it must travel with the extracted repo); the
  monorepo side retires through 6.1's source path with anchors
  remapped through the split map.
- The namespace-ownership statement written here, once, into the
  CLAUDE/README templates and the release-workflow doc (the full
  writer set from the decisions section, including the scrub
  exception).
- Verify (one per goal): converged-mirror release pushes exactly one
  tag and one Release through the module, branch untouched, exit
  zero (the regression for the deleted push); a mirror-step failure
  fixture completes the release nonzero with the healer named; both
  loader-error fixtures; publish-workflow-drop fixture; the go.mod
  rewrite present and tripwire-tolerated; the tags-dimension healing
  fixture; the Swift check fixture; the promotion fixture proves
  tree equality, persists the map, and every changelog hash AND
  anchor resolves in the promoted repo; step-marker completeness
  fixture; the templates carry the statement.

## Phase 10 — Docs, release, fleet sweep

Effort: extra large. Session conduct: run as one dedicated,
uninterrupted session; the restore command comes from the committed
file phase 0.0 wrote and the workspace list from run-time discovery
or the local-only file; an interruption is recoverable per-repo (the
script is idempotent — re-run on the remaining workspaces).

### 10.1 Documentation completion

- Mirror chapter corrections; root-member and tag-format reference;
  the workspace field table counts-free; the not-adopted paragraph
  states the concrete difference (release records as sole identity
  with tags as pure projections was NOT adopted; tags remain real
  refs the reconciler converges); regenerated CLI and schema
  surfaces; a final sweep for retired vocabulary in docs and help.
- Verify: docs build; freshness tests pass; grep for the retired
  workspace key and command names in non-historical docs is clean.

### 10.2 The workspace migration script

- Raw-file editing only: adds the root member entry (kind provided
  per repo by the operator, per the init rule), removes watch keys,
  relocates the mirror-remote key; plus invoking the anchor backfill
  per repo. Dry-run first, per-file reporting, idempotent.
- Verify: script fixtures for each edit; idempotency; a dry run
  changes nothing.

### 10.3 Quiescent verification

- Entire suite, all checks (networked checks fail-closed as ruled),
  docs build; one end-to-end conversion round-trip (extract from one
  fixture workspace, absorb into another) composing tag derivation,
  lineage, anchors, and remap; the shared-worktree log review.
- Verify: all green; the round-trip's every hash and anchor resolves.

### 10.4 The campaign release

- Changelog entries for the campaign per discipline; the release
  file; the release RUNS FROM THE WORKING TREE (uv run — the pinned
  installed rlsbl is the old version and must not perform this
  release).
- Verify: the release completes; the new version's archive carries
  its anchor; the Release body carries the marker.

### 10.5 Fleet sweep, then restore

- SWEEP FIRST: the migration script (from the checkout, uv run)
  across every fleet workspace except those whose conversion is
  deferred to their own todos; per-repo checks green. THEN restore
  the editable install (the committed restore command). Rewrite the
  rlsbl sections of the user-level CLAUDE files against the shipped
  surface.
- Verify: fleet-wide checks green in every non-deferred workspace;
  the deferred workspaces' state is exactly the documented
  pinned-release path; the editable install is restored last; the
  home-file rewrite is done.

## Deliberately not in this campaign

- Release records as sole identity (tags as projections): considered,
  not adopted.
- The four-place check-registration reduction: filed,
  todo/single-declaration-check-registration.md.
- selfdoc-side work (member-scoped docs transport, directive-failure
  hardening, stale-output reconciliation, flat posts layout): filed
  in selfdoc's todos.
- The deferred implicit-mode workspace conversions: their own filed
  todos, on the phase P release, on their own schedule.
- Per-repo normalization judgment: the affected repos' filed todos;
  the sweep executes the mechanical part.
