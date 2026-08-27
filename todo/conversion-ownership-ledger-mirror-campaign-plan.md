# Campaign plan: conversions, workspace ownership, release ledger, publication reconciler, release mirrors, target support matrix

Implementation plan for a single campaign across rlsbl. Every design
decision is made and ratified by the user; no phase contains open design
work. The plan was grounded against the working tree, then adversarially
critiqued (veracity, consistency, duplication, practice) and revised;
symbol anchors below name real code. Fleet-side normalization is part of
the FINAL phase here (it runs after the release, same session); the
per-repo todos already filed in the affected repos describe the judgment
calls those sweeps apply.

Campaign preconditions and lifecycle:

- Before any work: replace rlsbl's machine-wide editable install with a
  normal install of the last released version (standing rule in
  ~/Projects/CLAUDE.md, "Pin the install before campaigns on fleet
  tools"). Every repo on the machine keeps running released rlsbl while
  the campaign proceeds; mid-campaign hard errors therefore reach nobody.
- One release ships the whole campaign, at the very end. Intermediate
  phases accumulate as local commits on main.
- After the release: restore the editable install, then run the fleet
  workspace-migration sweep in the same session (final phase), so no
  fleet repo ever observes the upgrade errors.

## Terminology

- "Project" and "member" both mean a workspace entry in workspace.toml
  (the codebase says "project"; releasable membership is the `releasable`
  field). This plan says member for the workspace unit; executors should
  read it as the codebase's project.
- "The ledger" is not a new artifact: it is the set of anchored archived
  release files — TOML, at .rlsbl/releases/v{X.Y.Z}.toml or the
  releasable-state equivalent — once they carry the anchor fields. There
  is no separate store, no new format.
- "The publication reconciler" is the extended `release reconcile`
  command (phase 8). "The mirror reconciler" is `monorepo mirror`. The
  word tripwire alone always needs one of those qualifiers.
- The reserved root identity: every workspace's path-"." member carries
  the literal name `root`.

## Decisions this plan encodes

Rulings marked [%%] were adopted on trust of the session's recommendation
per the user's decision-origin convention: freely reversible, never to be
cited as deliberate intent. Unmarked rulings are the user's deliberate
decisions.

- The releasable is the portable unit of conversion. Extraction operates
  on releasables only. Absorb always targets a releasable, auto-creating
  a singleton for a bare package.
- Implicit mode (a workspace with no releasables section) is DELETED
  entirely. The migrate-releasable command and its module — restored in
  June 2026 with a commit message declaring them temporary — are deleted
  with it, after their two remaining uses (converting the last two
  implicit-mode fleet workspaces during the final-phase sweep). Only
  their history-corrupting defects are fixed first. Post-release, an
  implicit-mode workspace is a load-time hard error naming a migration
  script as the remedy.
- Extract is a verified move-out: the source copy is deleted only after
  the new repo's tree is proven identical by git tree-object comparison,
  taken after the history filter and before any state transforms.
  Deletion goes through saferm; when saferm is absent, rm is used only
  when the `--delete-with-rm` flag was passed (registered
  presence-optional: passing it enables the fallback, absence means
  saferm is required and its absence is a hard error naming both
  remedies). Extract is consequential. [%%]
- Conversion consent contract: plan/apply within one invocation — the
  plan is computed and shown, apply proceeds under the command's
  consequential consent (`--approve-consequential` answers it). No plan
  file for conversions; the file-driven plan is the publication
  reconciler's contract only.
- Absorb runs its history rewrite in a working clone under the
  monorepo's own .git/rlsbl/ directory (announced in the plan output),
  merges by fetch, and recovers from any crash by idempotent re-run:
  each completed step is detected — the merge by its trailer and source
  identity, tags by presence at the mapped commit, the workspace entry
  by content — and skipped.
- Imported historical tags take the destination releasable's tag format
  uniformly, plus exactly one boundary alias tag at the current version.
  A ref-name collision, and a version released on both sides, are hard
  errors at plan time. The auto-created singleton releasable's tag
  format is written explicitly, derived from the member's primary target
  scheme; a source whose targets span both schemes (path-prefixed and
  name-prefixed) is a plan-time hard error with an operator-declared tag
  format as the named remedy. [%%]
- Conversions record facts (predecessor/successor repo identity,
  old-to-new tag maps, departed tag globs, boundary aliases) in a
  committed lineage record in the releasable state directory. Follow-up
  obligations are standing checks derived from facts plus reality. The
  PyPI Trusted Publisher residual is discovered at first publish and
  recovered via release retry; BOTH conversion directions print the
  exact settings path when a PyPI target moved.
- Sibling manifests are never mutated by extract: inbound
  local-dependency edges are a hard refusal whose plan names the exact
  rewrite-command invocation per edge (registry floor at the locked
  version; a never-published floor version is its own hard error). The
  departing name joins internal_dep_floors. Registry dependencies
  between siblings are never auto-converted on absorb — covered by a
  test.
- Two standing hard errors in dev sync and dev status (ratified in the
  absorb round): an editable overlay whose package is a member of the
  current workspace, and an overlay whose path resolves inside the repo
  — each naming the one-line remediation.
- Manifest rewriting lives in a new `rewrite` command group:
  `rlsbl rewrite go-module-path` and `rlsbl rewrite uv-path-sources`,
  standalone plan/apply commands.
- rlsbl never acts on external systems; completion output prints
  next-step commands.
- Release ledger: every archived release file records the released
  commit (the CI-verified candidate SHA) and the tree hash of the
  released path (the releasable's subtree tree for workspace members,
  the root tree for standalone projects) — authored into the archive by
  the finalization step before the file is locked; an anchor appearing
  in an operator's unreleased.toml is a validation error (anchors are
  finalization-authored only). [%%]
- Ledger read semantics: "the latest release from here" means the
  highest anchored version whose commit is an ancestor of HEAD (what
  git describe meant), specified as a per-consumer table; absolute reads
  only where a version is named explicitly (undo, edit, deprecate,
  yank). Three errors, each at its site: DISAGREEMENT (the version's
  tag exists but points elsewhere than the anchor), INDETERMINABLE (the
  anchor commit is absent from truncated history — shallow clones per
  se are fine; the error names the deepen remedy), MISSING-ANCHOR (a
  pre-migration archive; the error prints the complete single-version
  recovery: the tag-derived value and the unlock/edit/relock steps —
  the one place rlsbl instructs manual state editing, re-validated by
  the next check run).
- Anchor backfill is a repo script (scripts/, the backfill_changelog.py
  precedent), dry-run first, idempotent, one-time and pre-reconciler
  (after phase 8 exists, tag repair belongs to the reconciler).
  Descriptions for the pre-description-era versions are authored
  MEANINGFULLY by subagents from each version's own changelog entries
  and commits — never mechanical placeholder text. The one tagless
  version: recover its release commit by history inspection, anchor it,
  and let the publication reconciler materialize the missing tag as a
  routine verdict; an explicit unanchorable marker only if recovery
  fails.
- Tags and GitHub Releases are a tool-owned derived namespace converged
  by the publication reconciler — the existing `release reconcile`
  command extended with the ledger (plus lineage records) as a second
  explanation source beside the rewrite journal: one command, one
  merged plan, per-version verdicts against the full expected-ref set
  (primary, companion, and RECORDED alias tags — from lineage and
  rename records, not one alias per version), leased writes, and a
  tripwire that hard-errors on any ref neither source explains,
  touching nothing. Consent is file-driven: plan writes a file, apply
  reads it, the command is consequential. Per-target materialization
  policy lives on the target protocol; the default policy is
  materialize; Go refuses any version whose recorded module path
  differs from the current one, and the published-evidence guard gains
  a module-proxy evidence source for Go (the tag-based probe is
  circular for tag adjudication; both sources combine under the
  existing fail-closed rule). [%%]
- The unpublished-refs standing check is reconciler-pointing: its
  errors name `release reconcile` as the remedy; missing-remotely is
  the reconciler's routine materialize work surfaced as a blocking
  finding, not a competing repair path.
- Namespace ownership (the never-push restatement, written once, in
  phase 9 where the writer set completes): branch heads on origin are
  written only by releases; origin tags and GitHub Releases only by the
  release flow's tag step and the publication reconciler; the mirror's
  branch only by the mirror reconciler's converge; mirror tags and
  mirror GitHub Releases only by the shared mirror-publication module
  (release step writes through it, mirror reconciler heals through it).
- Mirrors: destination is a releasable-level field (multi-member
  releasable with a mirror is a hard error); the release's mirror step
  invokes converge (leased) and publishes the converged tip's tag and
  Release through the shared module; the existing unforced main-branch
  push is deleted; go.mod joins the mirror's scaffold-owned path set
  and the scaffold commit rewrites it to the mirror identity; mirror
  scaffolds drop the publish workflow; a Swift member requires a
  configured mirror (registration-time hard error).
- Mirror promotion (extracting a mirrored releasable adopts the
  mirror's public history): the deletion proof is tree-hash equality
  between the monorepo subtree and the mirror's pre-scaffold split
  commit — subtree split preserves trees, so this is a plain equality
  check; the scaffold layer is acknowledged deliberate divergence. The
  split correspondence map is regenerated on demand and persisted with
  the lineage record (regeneration requires the monorepo, which
  promotion retires). [%%]
- Workspace ownership: every NON-TOOL-OWNED file has exactly one owning
  member — most specific member path wins; the mandatory root member
  (reserved name `root`) owns the remainder; the tool-owned exempt set
  is derived from STATIC path rules only (never from per-repo mutable
  registries). The watch key is deleted; a workspace file carrying it —
  like one still in implicit mode, or one carrying the relocated
  per-project mirror-remote key — is a LOAD-TIME hard error naming the
  migration script (loader-sited errors are safe: migrations are
  scripts editing raw TOML, never rlsbl commands, so no tool needs to
  load invalid state; the pinned install covers the mid-campaign
  window). [%%]
- CI triggering derives from declared dependency edges (all scopes)
  plus built-in rules: workspace-root manifest/lockfile changes trigger
  every member; release-machinery paths are auto-appended; a router
  change re-runs everything. Router filters gain negated excludes; the
  generator and the release-time simulation share one negation-aware
  matcher.
- Root-directory resolution: the root member resolves like any member
  for project-scoped commands; workspace-detection call sites are
  reclassified to detect the workspace explicitly. EXCEPTION, ruled
  deliberately: `release run` at any workspace root requires an
  explicit `--releasable <name>` selector — absent is a hard error
  listing the workspace's releasables. One uniform rule regardless of
  workspace shape; plain standalone repos are untouched.
- A commit whose changed files cannot be determined is a hard error
  naming the commit and the failing git operation.
- Root releasables must declare tag_format explicitly (hard error
  teaching the bare-version continuity choice).
- Target support: all targets stay; focus on the main ones. Every
  support axis becomes answerable by asking the target class; each
  feature declares one explicit unsupported-target policy; the
  capabilities set is deleted in favor of introspection; a generated
  committed target-matrix artifact with a freshness check feeds every
  docs table. The unknown-target test-runner policy: an explicit skip
  naming the target, surfaced in the release step summary (candidate
  CI remains the net). The four-place CHECK registration convention
  stays for this campaign (its reduction is filed:
  todo/single-declaration-check-registration.md).
- pgdesign's hardcoded schema-subdirectory detection fallback is
  deleted; the hard error fires at USE (version/schema resolution of a
  declared pgdesign target), never in detect(), which runs in every
  repository. General error-siting principle (executor convention
  below): errors fire where a feature is used, not where the world is
  scanned. [%%]
- Network-dependent checks are fail-closed: inconclusive is a hard
  error; rerun when the network is back. [%%]

## Phase dependency table

| Phase | Depends on |
|---|---|
| 0 | — |
| 1 | 0 |
| 2 | 0 |
| 3 | 0 |
| 4 | 0; shares files with 3 (see call-site assignment in 4.3) |
| 5 | 2 |
| 6 | 0, 1, 2, 3, 4, 5 |
| 7 | 0, 4, 6 (lineage facts) |
| 8 | 0 (skeleton), 4, 6 (lineage) |
| 9 | 3 (workspace schema), 4, 5 (Go rewrite), 6 (extract), 8 (verdict/policy shapes) |
| 10 | everything |

Shared call sites between 3 and 4 are assigned once: attribution
threading (the full-member-list rewrite) belongs to phase 3; version/
range anchor reads in the same files (status, unreleased, monorepo
status coverage, batch release init) belong to phase 4. Phase 3 executes
before phase 4 wherever both touch one function.

## Executor conventions

- Red-green for every defect fix.
- One commit per item via safegit; machine-generated files via rlsbl
  commit. Never push; one release at the end.
- Every new check registers in the four existing places together
  (implementation, rlsbl/data/checks.toml, CHECK_TARGETS, docs/checks.md
  row) — the freshness tests fail otherwise.
- Every new command updates the strictcli schema dump, regenerated docs,
  and the wiring/effects/consequential pinning tests.
- Error siting: a new hard error fires where the feature is used, never
  inside detection or parsing that runs everywhere.
- Repo-wide sweeps follow the scripts/ sweep precedent (dry-run, then
  expected-occurrence assertion, then full diff review).
- Subprocess helpers take explicit timeouts (the extract module's _run_git
  currently takes none — fix on touch).
- Every subphase goal bullet has a matching verify bullet.
- Authoritative test runs only on a quiescent tree.

---

## Phase 0 — Foundational groundwork

Effort: medium.

### 0.0 Pin the install

- Replace the machine-wide editable rlsbl install with a normal install
  of the last released version; record the exact restore command in the
  session notes for phase 10.
- Verify: `rlsbl --version` reports the released version; the editable
  path is no longer on the import path.

### 0.1 Conversion and root-member test fixtures

- Extend the conversion test helpers in tests/test_releasable_extract.py
  (which build the outdated per-package layout) to build explicit-mode
  workspaces with releasable state directories, reusing the
  multi_releasable_monorepo_factory machinery in tests/conftest.py.
- make_workspace already accepts path "." members; add releasables
  support and a root-member convenience.
- Verify: at least one existing conversion test re-expressed on the new
  fixtures passes; fixtures exercised by the suite.

### 0.2 Preview hygiene for conversions

- Fix the one bare `git status` (validate_absorb_preconditions in
  rlsbl/commands/monorepo/extract.py) to `--no-optional-locks`, matching
  every other call site. This precedes the phase 6 rebuild deliberately:
  developing 6.2's plan phase requires a working CLI-level dry run.
- Add a CLI-level regression test driving `monorepo absorb --dry-run`
  through the app with a bound effects context.
- Sweep the conversion modules for any other observation missing from
  the allowlist (rlsbl/observe_allowlist.py) — enumerate, don't sample.
- Verify: the CLI-level dry-run test passes; the allowlist tests stay
  green.

### 0.3 Shared plan/apply skeleton

- Lift the reusable shape from rlsbl/commands/monorepo/mirror_cmd.py
  into a shared module: a plan dataclass base (state string, observed
  facts, derived predicates), a per-state plan renderer, and the
  observe/branch/apply entry-point skeleton with the
  no-writes-above-this-line boundary convention. Constraints: all writes
  through rlsbl/effects.py; the module sits with other command-neutral
  libraries (not under commands/), importable by monorepo and release
  commands alike.
- Migrate mirror_cmd onto it behavior-identically. Consumers: extract
  and absorb (phase 6) and the publication reconciler (phase 8) — both
  phases' briefs name this module explicitly.
- Reconcile the three is_ancestor implementations (git_util,
  mirror_cmd, and the private _is_ancestor in changelog/validate.py —
  note their differing failure semantics) into one with an explicit
  indeterminable outcome.
- Verify: the entire mirror test module passes unchanged; one shared
  is_ancestor with tests for true/false/indeterminable.

### 0.4 Lineage fact records

- The committed lineage record in the releasable state directory:
  append-style JSON records of conversion events (absorbed-from,
  extracted-to, old-to-new tag maps, departed tag globs, boundary
  aliases, identity transitions, promotion split maps), following the
  undo-audit write pattern (rlsbl/evidence_gate.py write_undo_audit) and
  validated like other rlsbl documents.
- Read/write APIs; check-context access; new config keys added to
  validate_config_schema (rlsbl/config.py).
- Verify: unit tests for write, append, read, validation; a malformed
  record is a hard error.

---

## Phase 1 — Correctness fixes in code that survives the campaign

Effort: small (shrunk by the implicit-mode deletion ruling: only the
history-corrupting consolidation defects are fixed — the consolidation
functions survive by moving into the phase-10 migration script when the
command and module are deleted in 6.3, and they run exactly twice more,
in the final-phase fleet conversions).

### 1.1 migrate-releasable: history-corrupting defects only

- Preserve entry identifiers through consolidation, versioned-file
  merge, and dedup (rlsbl/releasable_migration.py consolidate_changelogs,
  _merge_versioned_files, _dedup_entries drop them — a permanent loss
  for the converted repos).
- Fix the truncation case: a releasable with unreleased entries and no
  member entries must not have its unreleased file overwritten empty.
- The remaining defects found in review (tag misreport, uncommitted
  output, workspace-wide cleanup, hardcoded tag glob) are NOT fixed:
  the tool is deleted in phase 6.
- Verify: red-green tests for both fixes; converted-fixture entries
  keep their ids.

### 1.2 Check-layer fixes

- workspace-unregistered becomes target-path-aware: a directory that is
  a declared target path of a registered member or releasable is exempt.
- Delete the pgdesign schema-subdirectory detection fallback
  (rlsbl/targets/pgdesign.py detect and its path resolvers); the hard
  error naming the explicit-path remedy fires in version/schema
  resolution — never in detect().
- (The companion-tag check fix moved to 2.1 — it is target-protocol
  work on a module phase 2 touches.)
- Verify: red-green for both; a repo with an undeclared schema
  subdirectory and NO pgdesign target declared detects nothing and
  errors nowhere.

---

## Phase 2 — Target support matrix

Effort: extra large.

### 2.1 Protocol migration of behavior-encoded axes

- First, the enumeration sweep: list every target-name literal used in
  a feature-support conditional (grounding found them across roughly
  forty modules — the seven known sites are a floor, not the list).
  The sweep output is the subphase's work list, committed with the
  change.
- Migrate each axis onto the target protocol or a registry-derived
  structure. Scope explicitly includes: the yank dispatch, the test
  runner (its unknown-target silent success becomes the surfaced skip
  per the ruling), the name-availability registries and chains,
  name-normalization, the check skip-sets, the lint dispatch family
  (_detect_languages, _create_linter, _create_import_scanner — the same
  silent-skip shape as the test runner, keyed on a parallel language
  taxonomy), the detection_files-vs-detect() double statement across
  targets, and the companion-tag check (which calls companion_tags()
  instead of hardcoding go and re-deriving the format).
- The plain target's hand-typed other-manifests list derives inside the
  targets package from the other targets' detection_files, PLUS a
  declared extras set for the two manifests belonging to no current
  target (Cargo.toml, selfdoc.json) so detection behavior does not
  silently change.
- The structural guard, concretely: a test asserting target-name
  literals in feature conditionals appear only in an allowlisted module
  set (the targets package and checks/__init__.py's CHECK_TARGETS,
  which stays per the registry ruling).
- Verify: the sweep list is fully ticked; behavior tests identical for
  supported targets, explicit skip/error for unsupported; the guard
  test passes; a plain-target fixture with a Cargo.toml still refuses
  to detect plain.

### 2.2 Capability derivation

- Delete the capabilities class attribute; derive per axis by
  introspection. Enumerate ALL current readers before deleting (the
  grounding found six, including two getattr-with-frozenset-default
  sites in pipelines/base.py and release execute that would silently
  degrade — the getattr default pattern is banned in the replacement).
- Correct the false docs claims (the pipelines doc's nonexistent
  capabilities; the name-consistency sentence in the targets doc).
- Verify: derivation tests replace the valid-capabilities test; a test
  proves the two probe-deciding sites consult the derivation (no
  silent-empty path).

### 2.3 The committed matrix artifact

- Generate rlsbl/data/target-matrix.json covering every axis from 2.1
  and 2.2; commit via rlsbl commit; freshness check (snapshot-check
  shape) registered four-place.
- Completeness both ways at import time: a new target must answer every
  axis; a new axis must cover every target.
- Verify: freshness check red on unregenerated change, green after; the
  import-time assertion fires on a synthetic incomplete target in a
  test.

### 2.4 Docs derivation

- Replace hand-written per-target tables with directives fed by the
  matrix; delete duplicates; switch hand-typed counts to the derived
  count directive; remaining prose counts-free.
- Verify: docs build; grep-style test that the deleted tables' section
  markers are gone.

---

## Phase 3 — Workspace ownership model

Effort: extra large. The root-attribution defect and watch deletion are
one atomic change (a root member currently matches nothing by prefix —
the computed "./" never matches git paths — so watch is every existing
root member's only territory).

### 3.1 Single-owner attribution core

- One ownership resolver: full member list + file path in, single owner
  out (most specific path wins; root residual; tool-owned exempt set
  from 3.7 excluded — the invariant is "every non-tool-owned file has
  exactly one owner").
- Rewrite git_util's file_matches_project, filter_commits_for_project,
  filter_commits_for_releasable, affected_projects around it; thread
  the full member list through every caller (the changelog validate
  scope filter; checks/_common's changelog-context helper and its
  consumers; prepush; changelog add's scope check and packages
  derivation; the workspace test-suite check; releasable migration's
  derivation until its deletion). Version-anchor reads in the same
  files belong to phase 4 (see the dependency table).
- Collapse impact's duplicate longest-prefix mapper onto the resolver.
- Unattributable commit = hard error naming the commit and operation
  (replacing the fail-open include-everywhere).
- Verify: property tests (exactly one owner per non-tool-owned file;
  most-specific wins; root residual); an unattributable-commit fixture
  hard-errors; impact/coverage/prepush/status suites pass on the new
  semantics; the property test's exemption for tool-owned paths matches
  3.7's function.

### 3.2 Mandatory root member and reserved identity

- load_workspace hard-errors (loader-sited, per the ruling) on: no
  path-"." member; a path-"." member named anything but `root` (the
  name auto-applies when omitted); a non-root member named `root`; a
  watch key anywhere; an implicit-mode workspace; the per-project
  mirror-remote key. Each error names the migration script.
- monorepo init scaffolds explicit mode: the root stanza plus a
  releasables section.
- Root members without a detectable root manifest are exempt from
  stale-entries and targets checks as dev nodes are.
- Job keys, filter keys, and check regexes derive from the reserved
  name via the existing root-publisher derivation in publish_inline,
  updated to consume `root` as the one convention.
- Regenerate the router in this subphase (the filter derivation changes
  here; do not leave it stale until 3.5's check lands).
- Verify: loader fixtures for every error with its message; init output
  contains the stanza; the derivation produces valid workflow YAML keys
  for a root member.

### 3.3 Root-directory command resolution

- Enumerate ALL resolve_project call sites by grep (grounding counted
  about thirty; the plan's earlier list of eleven was a sample) and
  classify each: project-scoped (resolves to the root member) vs
  workspace-scoped (switches to find_workspace_root).
- `release run` at any workspace root requires `--releasable <name>`;
  absent is a hard error listing the releasables. Member-directory and
  plain-standalone invocations unchanged.
- Verify: per-site test for each reclassified site; release-run
  fixtures at single-releasable AND multi-releasable roots (both
  require the selector; with it, both release the named releasable);
  check at the root still iterates every releasable.

### 3.4 watch deletion

- Remove the key from types, loader (the 3.2 error covers residue), add
  command and flag, list output, router generation; rewrite every
  user-facing text naming watch (changelog add scope errors, CI-check
  remediation, release empty-window remediation, docstrings feeding
  generated docs). Doc mentions outside historical changelogs are
  removed here, not deferred.
- Verify: a grep-style test pins that the key name survives only in
  the loader error message and historical changelog data.

### 3.5 Trigger derivation and router filters

- Filters derive from ownership + dependency territories (all scopes;
  monorepo sync gains a WorkspaceGraph construction — its
  WorkspaceError failure modes surface as command errors, not
  swallowed) + built-ins (workspace-root manifests/lockfiles trigger
  all; the machinery auto-append generalizes the finalize-artifact
  append; router-change reruns all) + negated excludes. The root
  member's pattern is the match-everything glob plus excludes.
- One negation-aware matcher shared by the generator and the
  release-time simulation (execute's _router_pattern_matches learns
  negation by becoming this matcher). The generator/CI boundary
  (picomatch) cannot share code: a conformance corpus pins the
  matcher's semantics against a recorded real paths-filter run,
  including negation ordering.
- New router-filters freshness check (regenerate-and-compare; nothing
  polices the filters block today).
- Verify: generator and simulator agree on the corpus; the freshness
  check reds on a stale filters block; the batch-release contract tests
  pass.

### 3.6 Root releasable tag format

- The Releasable tag-format field becomes explicit-or-absent (sentinel
  None); the loader hard-errors on a root-member releasable without an
  explicit tag_format; round-trip preserves explicit values.
- This is a sweep: the grounding counted well over a hundred
  tag_format references in source plus more in tests — enumerate,
  assert the count, review the diff.
- Audit the consumers of the parsed tag scheme for bare-version tags
  inside a workspace (status, coverage anchoring, rename, mixed-scheme
  guard with a Go root member).
- Verify: loader fixtures both ways; a root-releasable bare-version
  fixture round-trips through status and coverage; sweep count
  asserted.

### 3.7 Derived tool-owned exempt set

- One enumeration function from STATIC path rules only (the changelog
  exemption path rules generalized); never from per-repo mutable
  registries like managed-files.json.
- Verify: unit tests; 3.1's property test consumes this function;
  workspace-machinery commits remain coverage-exempt.

### 3.8 Test overhaul

- Replace the two vacuous root-member coverage tests with positive AND
  negative assertions; rewrite the watch-pinning tests; migrate the
  fixture helpers — this is the second enumerated sweep (make_workspace
  call sites plus literal workspace-file writers across the test tree;
  assert counts).
- Verify: full suite green on a quiescent tree; the two old vacuous
  tests demonstrably replaced (an uncovered root-owned commit fails).

---

## Phase 4 — Release ledger

Effort: extra large.

### 4.1 Anchored archives

- Optional anchor fields (released commit SHA = the CI-verified
  candidate; tree hash of the released path) added to
  .strictspec/release-file.schema.toml; validator regenerated;
  release_file reader binds them; an anchor present in unreleased.toml
  at release time is a validation error.
- The finalization step authors the anchor into the archive before
  chmod (the standalone path currently renames the operator's file
  verbatim — this adds the one authorship step); the batch synthesize
  path gains the fields directly; undo's unfinalize round-trips
  anchored archives; the schema-vs-code authority note in the schema
  header is updated (the freshness check polices schema-to-validator;
  the reader tests police schema-to-code).
- Verify: schema freshness; released fixture carries the anchor; undo
  restore/re-finalize; the operator-supplied-anchor fixture errors.

### 4.2 The backfill script and rlsbl's own history

- scripts/ script (backfill_changelog.py precedent): derives anchors
  from tags across all recognized schemes; unlock/edit/relock via the
  established writable pattern (note: the helper is JSONL-named —
  rename or add a sibling for TOML); stamps the missing format-version
  marker on pre-gate archives; materializes missing archives. Dry-run
  first with per-version reporting; idempotent; explicitly one-time and
  pre-reconciler (post-phase-8, tag repair belongs to the reconciler).
- rlsbl's own history workstream, sequenced before 4.3 and 4.4:
  subagents author meaningful descriptions for every archive-less
  pre-description-era version from that version's changelog entries
  and commits (never placeholder text); the tagless version's release
  commit is recovered by history inspection and anchored (the
  publication reconciler later materializes its tag; an explicit
  unanchorable marker only if recovery fails); the stray unexplainable
  tag and the local/remote tag delta found in review are reconciled so
  the new checks register green.
- Verify: script fixtures for pre-gate archives, a missing archive,
  and an old-scheme tag (anchors, stamped gate, and the hard-error
  lists respectively); second run changes nothing; rlsbl's own repo
  passes 4.4's check after the workstream.

### 4.3 The consumer switch

- Enumerate the consumer list BY GREP over describe/tag-list/rev-list
  version anchoring (the grounding's list — the last-tag primitive and
  callers, the coverage-range chokepoint, undo's commit-walk and
  predecessor lookup, both tag-list dialects, the unscoped describe
  display, watch's commit-to-version labeling, the destroyed-tag guard
  — is the floor; releasable_migration's dialect dies with the module).
- Implement the ruled semantics: the per-consumer table (ancestry-
  selected default; absolute for named versions), the three errors at
  their sites with their messages (disagreement; indeterminable naming
  the deepen remedy; missing-anchor printing the single-version
  recovery). release reconcile's _local_tags is explicitly the observe
  layer, not an authority.
- The destroyed-tag guard reads the ledger (a regression test encodes
  the dry-run false-positive scenario from the existing todo — the
  scenario restated in the test, not referenced).
- Verify: per-consumer fixtures where ledger and tags agree (pass) and
  disagree (error naming both); an indeterminable fixture (truncated
  history) errors with the remedy; the dry-run guard test passes.

### 4.4 The unpublished-refs standing check

- Renders every anchored version's expected-ref set via a new
  expected_refs(version) on the target protocol — the single authority
  for primary, companion, and recorded alias tags (from lineage and
  rename records; NOT one alias per version) — consumed here and by
  phases 8 and 9. Three distinct errors (missing-locally,
  missing-remotely, wrong-commit); inconclusive probe is an error;
  every error names `release reconcile` as the remedy
  (reconciler-pointing, per the decision).
- Verify: four-way fixtures; four-place registration; the error text
  names the reconciler.

---

## Phase 5 — Rewrite command group

Effort: medium.

### 5.1 rlsbl rewrite go-module-path

- New `rewrite` group. Plan/apply command: rewrites the go.mod module
  line (reader exists in utils; the writer is new) and sweeps import
  sites. The tree-sitter scanner returns rows without character spans —
  the rewrite is line-anchored (or the scanner gains spans); the
  equality-or-prefix module-match rule is extracted into ONE shared
  helper (it currently exists twice: import_scanners and
  dep_validation). Plan reports per-file occurrence counts; apply
  asserts them.
- Verify: fixture repo rewrites completely; count mismatch aborts;
  plan lists every file; the shared match helper has both former
  callers.

### 5.2 rlsbl rewrite uv-path-sources

- Generalizes the pypi build-time rewriter to the working tree:
  project dependencies, optional dependencies, dependency groups, and
  tool-uv-sources entries; floors at the locked version; unpublished
  floor is a hard error naming the release-first remedy; updates
  internal_dep_floors.
- Verify: workspace-source and path-source fixtures convert; the
  unpublished case errors; dep-floors passes after conversion.

---

## Phase 6 — Conversion rebuild as plan/apply

Effort: extra large. Built on the 0.3 skeleton.

### 6.1 Extract: verified move-out on releasables

- One extract command on releasables (the package-level command and the
  separate releasable variant collapse; extracting a package from a
  shared releasable directs the operator to split first). Plan
  previews: tag translations and deletions, collisions, the tree
  verification, inbound-dependency refusals with exact rewrite-command
  invocations, the lineage record, next-step hints (including the
  Trusted Publisher settings path when a PyPI target moves).
- Apply: clean-tree precondition; repo lock held; clone and filter;
  tree-object identity verification (post-filter, pre-transform); full
  state transplant (changes, version, archives with their anchors,
  config); tag translation with boundary tag and lineage record; then
  source side — saferm deletion (or rm under `--delete-with-rm`),
  workspace edit committed, router re-synced, snapshot regenerated,
  departed tag globs recorded, departing name added to
  internal_dep_floors. Consolidate the several clean-tree probes found
  in review onto the shared helper.
- Consequential (the pinning tests and README template updated with
  it). For a MIRRORED releasable, deletion verification uses the
  promotion proof (phase 9): tree equality against the mirror's
  pre-scaffold split commit.
- Verify (one per goal): round-trip coherence on releasable-layout
  fixtures; verification failure aborts naming both hashes; the
  deletion-consent matrix (saferm present / absent-with-flag /
  absent-without-flag); dependent-edge refusal names the phase-5
  command; floor-list write asserted; lineage record contents
  asserted; boundary tag present; lock held (concurrent-run fixture);
  source workspace passes all workspace checks after apply; PyPI hint
  printed when a pypi target moved.

### 6.2 Absorb: releasable-only with healing

- Absorb targets a releasable (auto-singleton; tag format written
  explicitly, derived from the primary target scheme; the mixed-scheme
  case is a plan-time hard error naming the operator-declared
  tag_format remedy). Validation parity with monorepo add (releasable
  existence, dependency names, remote host); scaffold and sync run in
  apply.
- Working clone under .git/rlsbl/, announced in the plan; uniform tag
  import with plan-time ref-name AND same-version collision errors,
  plus one boundary alias; fetched bare tags never deleted from under
  pre-existing monorepo tags; the remap report surfaced; archives
  migrated with anchors; versioned files written locked; the silent
  same-version merge skip becomes an error; idempotent healing with
  the three ruled detection predicates (merge trailer + source
  identity; tags at mapped commits; workspace entry content); repo
  lock; snapshot regeneration; next-step hints incl. the Trusted
  Publisher path.
- The two overlay hard errors in dev sync/dev status (ratified):
  overlay package in current workspace; overlay path inside the repo.
- A test pins that sibling registry dependencies are untouched by
  absorb (the never-auto-convert decision).
- Verify (one per goal): healing at ALL THREE kill points (after
  merge, after tags, after registration — each re-run completes
  without duplication); both collision fixtures; the mixed-scheme
  refusal fixture; releasable-layout absorb migrates archives and
  surfaces the remap report; singleton tag format asserted in the
  written workspace file; add-parity validations each have a fixture;
  clone path announced in plan output; overlay errors fire on
  contrived local-only files; the registry-dep non-conversion test;
  workspace checks pass after apply.

### 6.3 Implicit-mode deletion

- Delete the migrate-releasable command and module (the fixed
  consolidation functions move into the phase-10 migration script,
  which becomes their home) and their command-level tests; delete
  implicit-mode support everywhere it branches (absorb routing is
  already releasable-only from 6.2; cleanup's implicit refusal;
  coverage and changelog routing splits; workspace loading treats a
  missing releasables section as the 3.2 loader error). The straggler
  remedy is the workspace migration script (phase 10).
- Verify: a grep-style test that the implicit-mode branch points are
  gone; the loader fixture for a releasables-less workspace errors
  with the script-naming message; help counts and pinned command
  tables regenerate.

### 6.4 Conversion docs

- The narrative conversion chapter in docs/monorepo.md; regenerated
  CLI docs; the stale generated API pages for excluded packages
  removed (review found many, not one — enumerate by comparing
  generated pages against the generation config; the underlying
  selfdoc problem is filed in selfdoc's todo).
- Verify: docs build; the enumerated stale pages are gone; docs
  freshness tests pass.

---

## Phase 7 — Standing checks from facts

Effort: large (five networked checks, each four-place).

- go-module-identity: go.mod module path equals origin identity plus
  member subdirectory.
- dep-locks: lockfiles resolve current manifests (sibling of
  dep-floors).
- npm-token-presence via the GitHub API (the existing gh api GET
  allowlist pin likely covers the argv — verify; the policy of
  listing secret names was accepted with the check's ratification).
- old-repo-archived and go-deprecation-published: from lineage facts
  plus the GitHub API and the module proxy respectively (both proxy
  readers extend the existing registry client).
- All fail-closed (inconclusive = error), network-tagged,
  preflight-class.
- Verify (per check): pass, fail, and inconclusive fixtures; four-place
  registration; error messages name their remedies.

---

## Phase 8 — Publication reconciler

Effort: large.

- Extend `release reconcile` (on the 0.3 skeleton): one merged planner
  over an explanation-source list — the rewrite journal AND the ledger
  plus lineage records — emitting one per-version verdict set
  (materialize, already-correct, refuse-foreign,
  refuse-identity-mismatch); plan_reconcile's journal logic is rebuilt
  as one source, its fail-closed refusals becoming verdicts; the
  tripwire hard-errors on any ref no source explains, touching
  nothing; observe reuses snapshot_remote_refs plus one gh release
  listing; expected refs come from expected_refs(version).
- File-driven consent: plan writes a reviewable plan file; apply reads
  it; consequential.
- Materialization policy on the target protocol (a 2.x-style axis:
  default materialize; Go refuses recorded-module-path mismatches);
  the target-matrix artifact regenerates with the new axis and its
  freshness check re-passes.
- The published-evidence guard gains the Go module-proxy evidence
  source; both Go sources combine under the existing fail-closed rule.
- Verify (one per goal): verdict fixtures for all four classes; the
  tripwire fixture (unexplained ref aborts everything); a journal-only
  fixture reproduces current reconcile behavior through the merged
  planner; the Go identity-mismatch fixture is never materialized; the
  proxy evidence source's lag case cannot clear a deletion alone;
  plan-file round-trip; matrix regenerated.

---

## Phase 9 — Release mirrors

Effort: large.

- Mirror destination moves to the Releasable (loader + serialization +
  the relocated-key loader error from 3.2; multi-member releasable
  with a mirror is a hard error).
- One mirror-publication module: writes the converged tip's tag and
  the mirror GitHub Release (the existing unaccounted Release creation
  in the release flow moves into it); the release's mirror step
  invokes the mirror reconciler's converge (leased) then publishes
  through the module; the unforced main-branch push is deleted; step
  markers keep their trivially-done completeness contract.
- The mirror scaffold commit rewrites go.mod to the mirror identity
  (phase 5 rewriter); go.mod joins the scaffold-owned set (this widens
  the mirror tripwire's tolerance — deliberate); mirror scaffolds drop
  the publish workflow.
- The mirror reconciler's plan gains a tags dimension (missing release
  tags materialize from the ledger through the shared module).
- Swift-requires-mirror registration check.
- Promotion: extract of a mirrored releasable adopts the mirror's
  history; the deletion proof is tree equality against the pre-scaffold
  split commit; the split correspondence map is regenerated and
  persisted into the lineage record; the monorepo side then retires
  through 6.1's source path.
- The namespace-ownership statement (the never-push restatement) is
  written HERE, once, covering every writer: origin branches
  (releases), origin tags/Releases (release tag step + publication
  reconciler), mirror branch (mirror converge), mirror tags/Releases
  (the shared module) — into the CLAUDE/README templates and the
  release-workflow doc.
- Verify (one per goal): release against a converged mirror pushes
  exactly one tag and one Release through the module and leaves the
  branch to converge; the deleted main-push has a regression test
  (converged-mirror fixture release exits zero); both loader errors
  fixtures; scaffold drops the publish workflow (fixture); go.mod
  rewrite present in the scaffold commit and tolerated by the
  tripwire; the mirror reconciler heals a missing tag; the Swift check
  fixture; promotion fixture proves tree equality, persists the map,
  and the promoted repo's changelog hashes all resolve; the templates
  carry the ownership statement.

---

## Phase 10 — Docs, release, fleet sweep

Effort: medium.

- Remaining docs: mirror chapter corrections, root-member and
  tag-format reference, the workspace field table made counts-free,
  regenerated CLI and schema surfaces; the "not adopted" paragraph
  states the concrete difference (records-as-identity with tags as
  pure projections was NOT adopted; tags remain real refs the
  reconciler converges).
- The workspace migration script (scripts/): adds the root stanza,
  removes watch keys, converts implicit-mode workspaces (the two
  remaining fleet ones use it during the sweep), relocates the mirror
  remote key; raw-TOML editing, dry-run first, per-file reporting.
- Quiescent-tree full verification: entire suite, all checks
  (network checks fail-closed — an inconclusive network answer blocks,
  rerun when it clears, per the ruling), docs build.
- Changelog entries; the release file; the single release.
- Post-release, same session: restore the editable install; run the
  migration script across every fleet workspace — including the two
  implicit-mode conversions, which the script performs itself via the
  consolidation functions it inherited from the deleted module; re-run
  each repo's checks green.
- Verify: fleet-wide `rlsbl check` green in every workspace; no repo
  ever observed the upgrade errors.

## Deliberately not in this campaign

- Records-as-sole-identity (tags as pure projections) — considered,
  not adopted; the ledger-plus-reconciler architecture is the ratified
  stopping point.
- The four-place check-registration reduction — filed:
  todo/single-declaration-check-registration.md.
- selfdoc-side work (member-scoped docs transport, directive-failure
  hardening, stale-output reconciliation) — filed in selfdoc's todos.
- Per-repo normalization judgment (root member kind, watch-replacement
  decisions, conformance members) — carried by the affected repos'
  filed todos; the final-phase sweep executes the mechanical part.
