# Campaign plan: conversions, workspace ownership, release ledger, publication reconciler, release mirrors, target support matrix

Implementation plan for a single campaign across rlsbl. Every design
decision is made and ratified by the user; no phase contains open design
work. Each ruling is stated once, in the phase that implements it.

Campaign lifecycle (stated once; phases refer back here):

- FIRST: a pre-campaign patch release of rlsbl ships the consolidation
  fixes (phase P), so the version that deferred workspaces pin cannot
  corrupt their history. A separate natural release preceding the
  campaign, permitted by the published-artifact exception.
- Then the machine-wide editable install is replaced with a normal
  install of that patch release (standing rule in ~/Projects/CLAUDE.md,
  "Pin the install before campaigns on fleet tools").
- One campaign release ships everything else, at the very end.
  Intermediate phases accumulate as local commits on main.
- After the campaign release: the fleet workspace sweep runs FIRST (the
  migration script executes from the checkout via uv run), THEN the
  editable install is restored — so no fleet repo observes the upgrade
  errors, except the workspaces whose conversion is deliberately
  deferred to their own todos.

## Terminology

- "Member" and "project" both mean a workspace entry in workspace.toml
  (the codebase says project). "Package" in changelog-entry context
  means the member a change is attributed to.
- "The ledger" is the set of archived release files (TOML, under the
  releases state directory per project or releasable) once they carry
  the anchor fields. No separate store.
- A command's "preview" is its printed would-do assessment (the plan
  half of plan/apply).
- "The publication reconciler" is the extended `release reconcile`
  command (phase 8). "The mirror reconciler" is `monorepo mirror`. The
  word tripwire is always qualified with one of those.
- The reserved root identity: every workspace's path-"." member carries
  the literal name `root`.

## Standing constraints (rulings no single phase owns)

- Namespace ownership (documented by phase 9): origin branch heads —
  releases only; origin tags and GitHub Releases — the release tag step
  and the publication reconciler, through the shared Release helper
  where a Release is written; the mirror branch — the mirror
  reconciler's converge; mirror tags and Releases — the shared mirror
  publication module; the history-scrub force-push remains the one
  sanctioned rewrite write.
- The workspace ownership exempt set ("which paths never need a
  changelog owner"; static rules only) and the mirror tripwire's
  allowlist ("which paths may the scaffold commit touch on this
  mirror"; per-repo, registry-fed) are deliberately distinct
  predicates. Never unify them.
- On mirrors, only go.mod gets an identity rewrite; the other
  scaffold-owned manifests deliberately do not (their registry
  identities are repo-independent).
- rlsbl never acts on external systems it does not own (repository
  administration, registry configuration, releases in other repos);
  completion output prints next-step commands.

Decision origin: the rulings encoded in this plan are the user's
deliberate decisions, except these trust-adopted elements (freely
reversible, never to be cited as deliberate intent): extract's
consequential classification; the packages-narrowing; the retired-key
load-time errors; the singleton tag-format derivation and mixed-scheme
refusal; the root-member check exemptions; the in-file anchors on the
CI-verified candidate; the in-place backfill of read-only archives; the
file-driven reconciler consent; the mechanics bundle (Go proxy evidence
source, repo lock on conversions, reserved-name rules, reused job-key
derivation, persisted promotion split map).

## Phase dependency table

| Phase | Depends on |
|---|---|
| P | — |
| 0 | P |
| 1, 2, 3, 5 | 0 |
| 4 | 0, 2, 3 |
| 6 | 0–5 |
| 7 | 0, 6 |
| 8 | 0, 2, 4, 6 |
| 9 | 3, 4, 5, 6, 8 |
| 10 | everything |

Shared-file assignment: where phases 3 and 4 touch one function,
attribution threading is phase 3's edit and version/range anchor reads
are phase 4's; phase 3 lands first in every such file. Phases 4, 8, and
9 each add a target-protocol axis and therefore each regenerate the
committed matrix (phase 2's completeness assertion and freshness check
demand it).

## Executor conventions

- Red-green for every defect fix.
- One commit per item via safegit; machine-generated files via rlsbl
  commit. Never push; the campaign releases per the lifecycle above.
- Full suite green at every phase boundary.
- Every new check registers four-place (implementation, checks metadata
  registry, check-to-target matrix, docs table row).
- Every new command updates the schema dump, regenerated docs, and the
  wiring/effects/consequential pinning tests.
- Error siting: a new hard error fires where the feature is used, never
  inside detection or parsing that runs everywhere.
- Repo-wide sweeps: dry-run, expected-occurrence assertion, full diff
  review; enumeration output committed with the change.
- Subprocess helpers take explicit timeouts (the conversion module's
  git helper takes none — fix on touch).
- One verify item per goal; verify sections list only the non-obvious
  assertions beyond "one fixture per goal above".
- Before the release: review the log for commits from other sessions.

---

## Phase P — Pre-campaign patch release

Effort: small.

- Fix the two history-corrupting consolidation defects in the
  releasable migration module: entry identifiers dropped through
  consolidation/merge/dedup; truncation of a releasable's unreleased
  file when no member has entries.
- Release as a normal patch. The deferred implicit-mode workspaces'
  filed todos pin this release.
- Verify: converted-fixture entries keep their identifiers; the
  truncation case preserved.

## Phase 0 — Foundational groundwork

Effort: medium.

### 0.0 Pin the install

- Normal install of phase P's release replaces the editable install.
  Record the exact restore command in a committed file alongside this
  plan. The fleet workspace list is NOT committed (public repo; other
  projects' paths stay out): the sweep discovers workspaces at run
  time or reads a local-only file.
- Verify: version report shows the patch release; editable path off
  the import path; the committed file contains no foreign paths.

### 0.1 Conversion and root-member test fixtures

- Extend the conversion test helpers (currently building the outdated
  per-package layout) to build explicit-mode workspaces with releasable
  state directories, reusing the multi-releasable fixture factory; add
  releasables and root-member support to the shared make_workspace
  helper.
- Verify: at least one existing conversion test re-expressed on the new
  fixtures passes.

### 0.2 Preview hygiene for conversions

- Fix the one bare `git status` in absorb's precondition check to the
  allowlisted no-optional-locks form (needed now: the phase 6 rebuild
  iterates via CLI-level dry runs).
- CLI-level regression test driving absorb's dry run through the app
  with a bound effects context.
- Sweep the conversion modules for other non-allowlisted observations.
- Verify: the CLI-level dry-run test passes; allowlist tests green.

### 0.3 Shared preview/apply skeleton

- Lift the shared shape from the mirror command into a command-neutral
  module: a preview is an ordered list of keyed verdict items (subject
  key, state, observed facts, would-do action); one renderer; the
  observe/branch/apply entry skeleton with the
  no-writes-above-this-line convention. The mirror's whole-repo state
  becomes the one-item case. All writes through the effects chokepoint;
  module importable by monorepo and release commands. Consumers: mirror
  (migrated now), conversions (phase 6), publication reconciler
  (phase 8).
- Reconcile ALL ancestry-check implementations (the git-util helper,
  the mirror's copy, the private one in changelog validation, the
  inline one on the release candidate path) into one with an explicit
  INDETERMINABLE outcome. Mapping: each caller's fail-closed branch,
  except the changelog cache check, where recomputation is the safe
  branch.
- Verify: the mirror test module passes unchanged; per-caller mapping
  tests for true/false/indeterminable.

### 0.4 Lineage fact records

- Committed lineage record: append-style JSON in the releasable state
  directory (standalone successor repos: under their own .rlsbl state
  directory) recording conversion events, tag maps, anchor remaps,
  departed globs, boundary aliases, identity transitions, promotion
  split maps. Written via the undo-audit append pattern; validated
  like other rlsbl documents. Name the actual reading code paths for
  any new config keys.
- Verify: malformed record is a hard error; standalone-repo location
  round-trips.

## Phase 1 — Check-layer fixes

Effort: small.

- workspace-unregistered becomes target-path-aware: a directory that
  is a declared target path of a registered member or releasable is
  exempt.
- Delete the pgdesign schema-subdirectory detection fallback; the hard
  error naming the explicit-path remedy fires in version/schema
  resolution, never in detection.
- Verify: a repo with an undeclared schema subdirectory and no
  pgdesign target declared detects nothing and errors nowhere.

## Phase 2 — Target support matrix

Effort: extra large.

### 2.1 Protocol migration of behavior-encoded axes

- Enumeration sweep first: every target-name literal in a
  feature-support conditional; the committed output is the work list.
- Assignment rule: behavior invoked per-target becomes a protocol
  method; aggregate sets consumed elsewhere become registry-derived
  structures. Applied table covers at least: yank dispatch (method),
  test runner (method; unknown-target silent success becomes a skip
  naming the target, surfaced in the release step summary), name
  registries and normalization (derived), check skip-sets (derived),
  the lint dispatch family (same silent-skip shape on a parallel
  language taxonomy), the companion-tag check (calls the protocol
  method), and detection (declared detection files feed the detection
  method's default; content-inspecting targets keep overrides).
- The plain target's other-manifests list derives from the registry
  plus a declared extras set for manifests belonging to no current
  target, preserving detection behavior.
- Structural guard: a test that target-name literals in feature
  conditionals appear only in an allowlisted module set (the targets
  package plus the check-to-target matrix).
- Verify: behavior identical for supported targets, explicit
  skip/error for unsupported; a plain-target fixture with an
  extras-set manifest still refuses detection; the skip line appears
  in the release step summary fixture.

### 2.2 Capability derivation

- Delete the capabilities attribute; derive per axis by introspection.
  Enumerate ALL readers first; the attribute-read-with-default pattern
  is banned in the replacement (two probe-deciding sites would
  silently degrade).
- Correct the false docs claims (nonexistent capabilities in the
  pipelines doc; the name-consistency sentence in the targets doc).
- Verify: the two probe-deciding sites provably consult the
  derivation.

### 2.3 The committed matrix artifact

- One generator in the targets introspection module produces the
  committed matrix file covering every axis; freshness check
  (regenerate-and-compare); import-time completeness both ways (new
  target answers every axis; new axis covers every target).
- Verify: freshness red on unregenerated change; completeness fires on
  a synthetic incomplete target.

### 2.4 Docs derivation

- Re-point the existing docs directives to read the matrix file
  (removing their live-import path); delete the hand-written duplicate
  tables; switch hand-typed counts to derived counts; remaining prose
  counts-free.
- Verify: a directive test reads the file without importing the
  registry; deleted-table markers gone.

## Phase 3 — Workspace ownership model

Effort: extra large. 3.1 and 3.2 are one atomic cluster: a root member
currently matches nothing by prefix, watch is every existing root
member's only territory, and the entire fixture corpus is implicit-mode
and root-less — attribution fix, loader errors, and fixture sweep land
together or the suite is red mid-phase. Build 3.7's function first.

### 3.1 Single-owner attribution core

- One ownership resolver: full member list + file path → single owner
  (most specific member path wins; root residual; 3.7's exempt set
  excluded; invariant: every non-tool-owned file has exactly one
  owner). Fix the root path-prefix computation.
- Rewrite the git-util attribution functions around it; thread the
  full member list through every caller (changelog validation scope,
  the checks common context and consumers, prepush, changelog add's
  scope check and packages derivation — auto-derived packages lists
  narrow to single-owner attribution, manual broadening via changelog
  edit remains — status, unreleased, monorepo status coverage, batch
  init, the workspace test-suite check). Collapse impact's duplicate
  longest-prefix mapper and the cwd resolver's own longest-prefix rule
  onto the resolver.
- A commit whose changed files cannot be determined is a hard error
  naming the commit and operation.
- Verify: property tests (single ownership over non-tool-owned files;
  most-specific; residual; exemption matches 3.7); the narrowing test
  (spurious multi-claims gone, manual broadening works).

### 3.2 Root member, reserved identity, loader errors, fixture sweep

- THE loader-error enumeration (authoritative; the phase 10 script
  remedies its mechanical subset): no root member; a root member named
  anything but `root` (auto-applied when omitted); a non-root member
  named `root`; a watch key; an implicit-mode workspace; a root-member
  releasable without explicit tag_format. Three remedy classes in the
  error texts: the migration script for the mechanical errors; the
  pinned patch release and filed-todo path for implicit mode; the
  stated decision for the two operator-decision errors (name
  collision, tag format).
- `monorepo init` requires the root-member kind declaration (dev node,
  or member of a named releasable); absence is a hard error explaining
  both. Init scaffolds explicit mode with an empty releasables
  section.
- Root members without a root manifest: exempt from the targets check
  (copied filtering) and the stale-entries check (new filtering — it
  has none today).
- Job/filter/check-regex derivation for the reserved name adopts the
  existing root-publisher derivation.
- The fixture sweep, same subphase: centralize the shared workspace
  fixture helper, then sweep the hand-written workspace-file fixtures
  across the test tree onto the new model.
- Regenerate the committed router (provisional until 3.5's freshness
  check).
- Verify: loader fixtures per enumerated error with message content
  asserted; init fixtures; both exemptions; derivation yields valid
  workflow keys; suite green at subphase end.

### 3.3 Root-directory command resolution

- Enumerate ALL cwd-resolution call sites by grep and classify:
  project-scoped (resolves to the root member) vs workspace-scoped
  (explicit workspace detection). Contract change: resolution inside a
  workspace never returns nothing.
- `release run` at any workspace root requires `--releasable <name>`;
  absent errors listing the releasables. Member-directory and plain
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
  (property accessor, dict reads, the add flag) — not the word itself,
  which legitimately remains as a release flag, a command name, and in
  the migration script and loader error.

### 3.5 Trigger derivation and router filters

- Filters derive from ownership + dependency territories (all scopes;
  sync gains a workspace-graph construction whose failures surface as
  command errors) + built-ins (workspace-root manifests/lockfiles
  trigger all; machinery auto-append generalizing the
  finalize-artifact append; router change reruns all) + negated
  excludes. The root member's pattern: match-everything plus excludes.
- One negation-aware matcher shared by generator and release-time
  simulation, validated against a committed verdict fixture captured
  once from the real CI filter library by a hand-run Node script,
  keyed to the pinned filter-action version; a check demands
  regeneration when the pin changes. New router-filters freshness
  check (regenerate-and-compare).
- Verify: generator and simulator agree on the fixture including
  negation ordering; a derived-filter observation test; freshness reds
  on a stale block; batch-release contract tests pass; fixture-to-pin
  linkage test.

### 3.6 Root releasable tag format mechanics

- The tag-format field becomes explicit-or-absent (sentinel); the
  loader error is enumerated in 3.2; round-trip preserves explicit
  values. Reference sweep across source and tests.
- Audit parsed-scheme consumers for bare-version tags inside a
  workspace (status, coverage anchoring, rename, mixed-scheme guard
  with a Go root member).
- Verify: a bare-version root releasable passes status and coverage.

### 3.7 Derived tool-owned exempt set

- One enumeration function from static path rules only (generalizing
  the changelog exemption rules).
- Verify: workspace-machinery commits remain coverage-exempt.

### 3.8 Remaining test overhaul

- Replace the two vacuous root-member coverage tests with positive AND
  negative assertions; rewrite the watch-pinning tests; remove the
  inert conftest fixture setting a timeout environment variable rlsbl
  no longer reads.
- Verify: an uncovered root-owned commit fails coverage.

## Phase 4 — Release ledger

Effort: extra large.

### 4.1 Anchored archives

- Optional anchor fields (CI-verified candidate SHA; tree hash of the
  released path — member subtree in workspaces, root tree standalone)
  in the release-file schema; validator regenerated; reader binds
  them. An anchor in the pre-release editable file is a hard error at
  release validation; undo's unfinalize strips anchors on restore.
- Finalization authors the anchor into the archive before locking (the
  one new authorship step on the rename path); the batch synthesize
  path gains the fields directly. Authority statement: the anchor is
  authoritative; the Release-body marker (which CI's publish workflow
  parses) is its projection — enforced by the shared helper built in
  phase 8; until then the release flow's existing marker write stands.
  Schema-header authority note updated; reader tests police the
  schema-to-code edge.
- Verify: released fixture carries both fields with the correct tree;
  undo strips and re-release re-authors; the editable-file anchor
  fixture errors.

### 4.2 The backfill script and rlsbl's own history

- The script (backfill precedent): anchors from tags across all
  recognized schemes; unlock/edit/relock via the established writable
  pattern (rename the JSONL-named helper or add a TOML sibling);
  stamps the missing format-version marker on archives predating it;
  materializes missing archives from recoverable sources. One-time and
  pre-reconciler; also runs per-repo during the phase 10 sweep.
- rlsbl's own history, split for parallelism: (a) description
  authoring — subagents write meaningful descriptions for every
  archive-less version predating descriptions, from that version's
  changelog entries and commits; (b) the anchor pass — including the
  tagless version (history-inspection recovery; the reconciler
  materializes its tag later; explicit unanchorable marker only on
  failure) and the one recognizable-but-foreign tag (a fourth taxonomy
  bucket: recognized scheme, no matching version — resolved or
  explicitly recorded).
- Verify: fixtures for a marker-less archive, a missing archive, and
  an unrecognizable tag (anchor+marker, materialized archive,
  operator-input error list; a recognized old-scheme tag anchors
  normally); second run changes nothing; rlsbl's own repo passes 4.4's
  check after the workstream, including the foreign-tag disposition.

### 4.3 The consumer switch

- Consumers enumerated by grep over describe/tag-list/rev-list version
  anchoring (known floor: the last-tag primitive and callers, the
  coverage-range chokepoint, undo's commit-walk and predecessor
  lookup, the tag-list dialects, the unscoped describe display,
  watch's commit labeling, the destroyed-tag guard, extract's
  tag-decision reads). The reconcile command's local tag listing is
  the observe layer, not an authority.
- Semantics map by QUESTION: every unreleased range and coverage
  computation selects the highest anchored version whose commit is an
  ancestor of the checkout; every latest-release FACT display uses the
  absolute highest, visibly annotated when the checkout predates it;
  preparing a release on a checkout lacking the latest anchored
  release is a hard error. Three read errors at their sites:
  DISAGREEMENT (tag exists, points elsewhere than the anchor);
  INDETERMINABLE (needed commit absent from truncated history;
  shallowness itself is fine; error names the deepen remedy);
  MISSING-ANCHOR (pre-migration archive; error prints the complete
  single-version recovery — tag-derived value, unlock/edit/relock —
  re-validated by the next check run). The destroyed-tag guard reads
  the ledger; a regression test encodes its dry-run false-positive
  scenario.
- Verify: per-consumer agree/disagree/indeterminable fixtures; the
  release-prep divergence error and the annotated fact display
  fixtures; the guard's dry-run test.

### 4.4 expected_refs and the unpublished-refs check

- expected_refs(version, context) on the target protocol: the single
  authority for a version's full ref set — primary, companions
  (absorbing the companion-tag collector's publish-mode and
  path-scheme rules; the collector is deleted), and recorded aliases
  from lineage/rename records. The release tag step consumes it. The
  three shipped warn-severity tag/Release checks are deleted as
  superseded. (A protocol axis — the matrix-regeneration obligation is
  owned by the shared-file assignment paragraph.)
- The unpublished-refs standing check renders it: three distinct
  errors (missing-locally, missing-remotely, wrong-commit);
  inconclusive probe is an error; every error names release reconcile.
- Verify: the release tag step's pushed set equals expected_refs on a
  Go companion fixture and a renamed-releasable alias fixture; deleted
  checks gone from registry and docs.

## Phase 5 — Rewrite command group

Effort: medium.

### 5.1 rlsbl rewrite go-module-path

- New rewrite group. Preview/apply: rewrites the module line (writer
  is new) and sweeps import sites line-anchored via the tree-sitter
  scanner. Consolidate the module-prefix match rule into ONE shared
  helper — sweep ALL copies (the import scanner, the dependency
  validator, and the CLI-detection module, whose second inline copy
  lacks the separator and wrongly prefix-matches similarly named
  modules; red-green that fix). Preview reports per-file occurrence
  counts; apply asserts them.
- Verify: fixture repo rewrites completely; count mismatch aborts; all
  former callers use the shared helper; the separator bug's regression
  test.

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

- One extract command on releasables (the package-level command and
  the separate releasable variant collapse). Preview: tag translations
  and deletions, collisions, per-member tree verification, inbound
  dependency refusals with exact rewrite invocations, the lineage
  record, next-step hints (Trusted Publisher settings path when a PyPI
  target moves; discovered-at-publish is recovered via release retry).
  Preview-time refusals: a mirrored releasable (until phase 9 wires
  promotion); the releasable containing the root member (the source
  would lose its root — restructure first); extraction from a shared
  releasable (points at the manual split procedure in the conversion
  chapter, 6.4).
- Apply: clean tree; repo lock; clone and filter; PER-MEMBER
  tree-object identity verification (multi-member filters union paths;
  each member's source subtree is verified against its destination
  tree); full state transplant including archives; anchor remapping
  through the filter commit map, recorded in the lineage record; tag
  translation with boundary tag; destination synthesis (the new
  workspace's root member entry and the extracted releasable's
  explicit tag format, so the new repo passes its own loader); source
  side — saferm deletion (rm only under `--delete-with-rm`;
  presence-optional; saferm absent without it is a hard error naming
  both remedies), workspace edit committed, router re-synced, snapshot
  regenerated, departed globs recorded (departed tags themselves stay,
  explained by the record), departing name added to
  internal_dep_floors. Consolidate the several clean-tree probes onto
  the shared helper.
- Consequential; the old not-consequential comment and pinning test
  updated in this change.
- Verify beyond per-goal fixtures: round-trip coherence with anchors
  resolving post-conversion (tree hashes as cross-check); verification
  failure names both hashes; the deletion-consent matrix (saferm
  present / absent-with-flag / absent-without-flag); destination
  passes its own loader; source workspace checks green.

### 6.2 Absorb: releasable-only with healing

- Absorb targets a releasable (auto-singleton; tag format written
  explicitly, derived from the member's primary target scheme;
  targets spanning both schemes are a preview-time hard error naming
  an operator-declared tag format as the remedy). Validation parity
  with monorepo add; scaffold and sync in apply.
- Working clone under the monorepo's .git/rlsbl/, announced in the
  preview; merge by fetch; uniform tag import with ref-name AND
  same-version collisions as preview-time errors, plus exactly one
  boundary alias at the current version; fetched bare tags never
  deleted from under pre-existing monorepo tags; the remap report
  surfaced; archives migrated with anchors remapped through the commit
  map (recorded in lineage); versioned files written locked; the
  silent same-version merge skip becomes an error; idempotent re-run
  healing (completed steps detected: the merge by trailer plus source
  identity, tags at mapped commits, the workspace entry by content);
  repo lock; snapshot regeneration; next-step hints including the
  Trusted Publisher path.
- The two overlay hard errors in dev sync and dev status: an overlay
  whose package is a member of the current workspace; an overlay whose
  path resolves inside the repo.
- A test pins that sibling registry dependencies are untouched by
  absorb.
- Verify beyond per-goal fixtures: healing at all three kill points
  (re-run completes without duplication); anchors resolve
  post-conversion; locked file modes asserted; workspace checks green
  after apply.

### 6.3 Implicit-mode deletion

- Delete the migrate-releasable command, module, and tests outright
  (deferred conversions run on the phase P release per their filed
  todos). Delete implicit-mode support everywhere it branches:
  cleanup's refusal, coverage and changelog routing splits, batch
  release's implicit-mode release-file sections, the loader (erroring
  per 3.2 with the pinned-release remedy).
- Verify: a grep-style test that the branch points are gone; help
  counts and pinned command tables regenerate.

### 6.4 Conversion docs

- The narrative conversion chapter (semantics, tag policy,
  verification, the follow-up model, and the manual split procedure
  6.1's refusal points at); regenerated CLI docs; stale generated API
  pages for excluded packages removed (enumerate by comparing
  generated pages against the generation configuration); the
  implicit-mode sections deleted from the monorepo guide.
- Verify: the split procedure exists at the location the refusal
  cites; enumerated stale pages gone.

## Phase 7 — Standing checks

Effort: large.

- go-module-identity (local): go.mod module path equals origin
  identity plus member subdirectory.
- dep-locks (local): lockfiles resolve current manifests (sibling of
  dep-floors).
- npm-token-presence (networked, GitHub API; the existing GET
  allowlist pin covers the argv — verify, else add a pinned entry).
- old-repo-archived and go-deprecation-published (networked; from
  lineage facts plus the GitHub API and the module proxy; both proxy
  readers extend the existing registry client).
- Networked checks fail-closed (inconclusive = hard error) and
  network-tagged; local checks are ordinary error checks.
- Verify: pass and fail fixtures per check; inconclusive fixtures for
  the networked three only; remedies named in error text.

## Phase 8 — Publication reconciler

Effort: large.

- Extend release reconcile (already consequential) on the 0.3
  skeleton: one merged preview over the explanation-source list — the
  rewrite journal, the ledger, lineage records, and the committed
  scrub archives (the journal lives under .git and does not survive a
  fresh clone; the archives do) — emitting per-version verdicts:
  materialize, already-correct, re-point-with-lease (wrong-commit
  repair), refuse-foreign, refuse-identity-mismatch. The journal logic
  becomes one source; the publication tripwire hard-errors on any ref
  no source explains, touching nothing; observe reuses the remote-ref
  snapshot plus one Release listing; expected refs from expected_refs.
- The shared Release-writing helper is built here: notes, the
  released-commit marker, and the prerelease flag emitted from the
  ledger; the release flow and the reconciler adopt it (phase 9 adds
  the mirror module).
- File-driven consent: preview writes the plan file; apply reads it.
- Materialization policy as a protocol axis (default materialize; Go
  refuses recorded-module-path mismatches). The published-evidence
  guard gains the Go module-proxy source; both Go sources combine
  under the existing fail-closed rule.
- Verify beyond per-goal fixtures: verdict fixtures for all five
  classes; the tripwire fixture (unexplained ref aborts everything); a
  journal-only fixture reproduces current behavior; a fresh-clone
  fixture explains a scrubbed ref via the committed archive;
  helper-emitted Releases carry the marker from both callers; proxy
  lag alone can never clear a deletion.

## Phase 9 — Release mirrors

Effort: large.

- Mirror destination on the Releasable (loader, serialization; the
  old per-project key becomes a loader error in this same change;
  multi-member-with-mirror is a hard error).
- One mirror publication module using the phase 8 Release helper; the
  release's mirror step invokes the mirror reconciler's converge
  (leased) then publishes the converged tip's tag and Release through
  it; the old unforced main-branch push is deleted; a mirror-step
  failure is non-fatal — recorded, the release completes, the exit
  status names the healers; step markers keep the completeness
  contract (trivially-done markers on no-op paths).
- The scaffold commit's go.mod rewrite (phase 5 rewriter) and the
  Swift-requires-mirror registration check are protocol axes (keeping
  2.1's guard green); go.mod joins the mirror's scaffold-owned set;
  mirror scaffolds drop the publish workflow.
- The mirror reconciler's preview gains a tags dimension (missing
  release tags materialize from the ledger through the module).
- Promotion, replacing 6.1's mirrored-releasable refusal: adopts the
  mirror history; deletion proof is tree-hash equality between the
  monorepo subtree and the mirror's pre-scaffold split commit; the
  split map is persisted into the lineage record (it travels with the
  extracted repo); the monorepo side retires through 6.1's source path
  with anchors remapped through the split map.
- The namespace-ownership statement (see standing constraints) written
  into the CLAUDE/README templates and the release-workflow doc.
- Verify beyond per-goal fixtures: converged-mirror release pushes
  exactly one tag and one Release through the module, branch
  untouched, exit zero; a mirror-step failure fixture completes the
  release nonzero naming the healers; the promotion fixture proves
  tree equality, persists the map, and every changelog hash AND anchor
  resolves in the promoted repo.

## Phase 10 — Docs, release, fleet sweep

Effort: extra large. Session conduct: one dedicated, uninterrupted
session; the restore command from 0.0's committed file; the workspace
list from run-time discovery or the local-only file; interruption is
recoverable per-repo (the script is idempotent — re-run on the
remaining workspaces).

### 10.1 Documentation completion

- Mirror chapter corrections; root-member and tag-format reference;
  the workspace field table counts-free; the not-adopted paragraph
  states the concrete difference (release records as sole identity
  with tags as pure projections was NOT adopted; tags remain real refs
  the reconciler converges); regenerated CLI and schema surfaces; a
  sweep for retired vocabulary in docs and help.
- Verify: grep for the retired workspace key and command names in
  non-historical docs is clean.

### 10.2 The workspace migration script

- Raw-file editing only: adds the root member entry (kind provided per
  repo by the operator), removes watch keys, relocates the
  mirror-remote key; invokes the anchor backfill per repo.
- Verify: fixtures per edit; idempotency; dry run changes nothing.

### 10.3 Quiescent verification

- Entire suite, all checks, docs build; one end-to-end conversion
  round-trip (extract from one fixture workspace, absorb into another)
  composing tag derivation, lineage, anchors, and remap; the
  shared-worktree log review.
- Verify: the round-trip's every hash and anchor resolves.

### 10.4 The campaign release

- Changelog entries per discipline; the release file; the release runs
  FROM THE WORKING TREE via uv run (the installed rlsbl is the old
  pinned version and must not perform this release).
- Verify: the new version's archive carries its anchor; the Release
  body carries the marker.

### 10.5 Fleet sweep, then restore

- Sweep first: the migration script (uv run, from the checkout) across
  every fleet workspace except those deferred to their own todos;
  per-repo checks green. Then restore the editable install. Rewrite
  the rlsbl sections of the user-level CLAUDE files against the
  shipped surface.
- Verify: fleet-wide checks green in every non-deferred workspace; the
  editable install restored last; the home-file rewrite done.

## Deliberately not in this campaign

- Release records as sole identity (tags as projections): considered,
  not adopted.
- The four-place check-registration reduction: filed,
  todo/single-declaration-check-registration.md.
- selfdoc-side work (member-scoped docs transport, directive-failure
  hardening, stale-output reconciliation, flat posts layout): filed in
  selfdoc's todos.
- The deferred implicit-mode workspace conversions: their own filed
  todos, on the phase P release, on their own schedule.
- Per-repo normalization judgment: the affected repos' filed todos;
  the sweep executes the mechanical part.
