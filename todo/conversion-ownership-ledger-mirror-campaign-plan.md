# Campaign plan: conversions, workspace ownership, release ledger, publication reconciler, release mirrors, target support matrix

This is the implementation plan for a single campaign across rlsbl. Every
design decision it encodes has been made and ratified by the user; no phase
contains open design work. The plan was grounded against the working tree
(file and symbol anchors below name real code). Fleet-side normalization work
is out of scope here — it is filed as todos in the affected repos and
activates only after this campaign's release.

One release ships the whole campaign, at the very end. Intermediate phases
accumulate as local commits on main.

## Decisions this plan encodes (stated in full, so the plan is self-contained)

- The releasable is the portable unit of conversion. Extraction operates on
  releasables only; absorb always targets a releasable, auto-creating a
  singleton for a bare package; implicit-mode workspaces (no releasables
  section) are refused by conversion commands with a pointer to
  migrate-releasable.
- Extract is a verified move-out: the source copy is deleted only after the
  new repo's tree is proven byte-identical (git tree-object comparison,
  taken after the history filter and before any state transforms). Deletion
  goes through saferm; when saferm is absent, rm is used only when a
  dedicated `--delete-with-rm` flag was passed;
  saferm absent without the flag is a hard error naming both remedies. The
  flag is never defaulted. Extract becomes consequential.
- Absorb runs its history rewrite in a working clone under the monorepo's
  own `.git/rlsbl/` directory (announced in the plan output), merges by
  fetch, and recovers from any crash by idempotent re-run: each completed
  step is detected (the merge by its trailer and source identity, tags by
  presence at the mapped commit, the workspace entry by content) and
  skipped. No state file, no resume command.
- Imported historical tags take the destination releasable's tag format
  uniformly, plus exactly one boundary alias tag at the current version.
  A ref-name collision, and a version released on both sides, are hard
  errors at plan time. The auto-created singleton releasable's tag format
  is written explicitly into the workspace file, derived from the member's
  primary target scheme (path-prefixed for Go, name-prefixed otherwise);
  a source whose targets span both schemes is a plan-time hard error with
  an operator-declared tag format as the named remedy.
- Conversions record facts (predecessor/successor repo identity, tag maps,
  departed tag globs) in a committed lineage record in the releasable state
  directory; follow-up obligations are standing checks derived from those
  facts plus reality — never a self-attested list. The one unverifiable
  residual (a moved PyPI package's Trusted Publisher configuration) is
  discovered at first publish and recovered via release retry; the
  conversion output prints the exact settings path.
- Sibling manifests are never mutated by extract: inbound local-dependency
  edges are a hard refusal, and the plan names the exact composable rewrite
  command invocation per edge (registry floor at the locked version;
  a never-published floor version is its own hard error). The departing
  package's name is added to the internal dependency-floor list. Registry
  dependencies between siblings are never auto-converted on absorb.
- Manifest rewriting lives in standalone, individually previewable
  commands: a Go module-path rewrite (module line plus repo-wide import
  sweep) and a uv path-source-to-registry-floor rewrite.
- rlsbl never acts on external systems (GitHub administration, registry
  configuration, releases driven in other repos); completion output prints
  next-step commands.
- Release identity gains a ledger: every archived release file records the
  released commit (the CI-verified candidate) and its tree hash; a one-time
  backfill derives anchors for existing versions; every internal consumer
  that resolves versions or ranges via git tags switches to the ledger,
  with tags demoted to a cross-check that hard-errors on disagreement.
- Tags and GitHub Releases become a tool-owned derived namespace converged
  by a fail-closed publication reconciler (observe, per-version verdicts
  from the ledger against the full expected-ref set — primary plus
  companion plus alias tags — leased writes, hard-error tripwire on any
  ref the ledger cannot explain, per-target materialization policy under
  which a Go version whose recorded module path differs from the current
  one is never materialized). The convergence lives in the existing
  release reconcile command, which gains the ledger as a second
  explanation source beside the rewrite journal — one command, one merged
  plan, the tripwire consults both sources before declaring a ref
  foreign. Consent is file-driven: plan writes a file,
  apply reads it, the command is consequential. The never-push rule is
  restated as namespace ownership: branch heads are written only by
  releases; tags and GitHub Releases are written only by the reconciler
  and the release flow's own tag step.
- Mirrors: the mirror destination becomes a releasable-level field (a
  multi-member releasable with a mirror is a hard error, because a subtree
  split is single-prefix). The mirror reconciler's converge logic is the
  only writer of the mirror's branch. The release's mirror step invokes
  converge (leased) and then tags the converged tip — whose tree contains
  the scaffold rewrites, including the mirror-identity go.mod — pushing
  only that tag. The existing unforced main-branch push in the release
  flow is deleted (it is live-broken against converged mirrors). go.mod
  joins the mirror's scaffold-owned path set. Mirror scaffolds drop the
  publish workflow. A Swift member in a workspace is legal only with a
  mirror configured (registration-time hard error otherwise). Extraction
  of a mirrored releasable adopts the mirror's public history rather than
  producing a new lineage.
- Workspace ownership: every file has exactly one owning member — most
  specific member path wins, and a mandatory root member (path ".") owns
  the remainder. The root member carries the reserved literal name `root`
  (auto-applied; a different explicit name on a root member, or a non-root
  member named `root`, is a hard error). The watch key is deleted; a
  workspace file still carrying it is a hard error with a migration
  message, never a silent ignore. CI triggering derives from declared
  dependency edges (all scopes trigger) plus built-in rules: a
  workspace-root manifest or lockfile change triggers every member,
  release-machinery paths are auto-appended to every filter, and a router
  change re-runs everything. Router filters gain negated excludes for
  other members' territories, and the release-time filter simulation
  shares one matcher with the generator. Root releasables must declare
  tag_format explicitly (a missing one is a hard error whose message
  teaches the bare-version continuity choice).
- Root-directory command resolution: the root member resolves like any
  member for project-scoped commands; call sites that used empty
  resolution as their workspace-root detection are reclassified to detect
  the workspace explicitly, so workspace-wide commands keep meaning the
  whole workspace.
- A commit whose changed files cannot be determined is a hard error naming
  the commit and the failing git operation — never fail-open attribution.
- Target support: all targets stay; support focus is the main ones. Every
  per-target support axis becomes answerable by asking the target class
  (the name-keyed if/elif chains, inline sets, and hand-typed lists
  migrate onto the target protocol or derive from the registry); each
  feature declares one explicit unsupported-target policy (hard error or
  explicit skip — no silent passes); the capabilities set is deleted in
  favor of introspection; a generated, committed target support matrix
  artifact with a freshness check feeds every docs table, and hand-written
  duplicates are deleted.
- Backfill descriptions for versions lacking release archives are
  recovered from existing sources (CHANGELOG, GitHub Release notes); when
  nothing exists, the backfill hard-errors listing the versions that need
  operator-supplied text. Nothing is fabricated.

Decision-origin note: the grounding-derived consequences in this list —
extract's consequential classification, the packages-field narrowing, the
two retired-key hard errors, the singleton tag-format derivation with its
mixed-scheme plan-time refusal, the root-member check exemption, the
in-file anchors on the CI-verified candidate, the in-place backfill of
read-only archives, the recover-or-error backfill descriptions, the
file-driven reconciler consent, and the mechanics bundle — were adopted on
trust of the session's recommendation ([%%]); they are freely reversible
and must never be cited as the user's deliberate intent. The rulings named
before them (releasable unit, move-out, ledger-through-reconciler,
release mirrors, ownership model, root resolution, unattributable-commit
hard error, mirror ownership contract, merged reconciler surface, the
surfaced test skip, and every name) are the user's deliberate decisions.

## Executor conventions

- Red-green for every defect fix: reproduce with a failing test first.
- One commit per item via safegit; machine-generated files via rlsbl
  commit. Never push; the campaign releases once at the end.
- Every new check registers in four places together: the implementation
  module, the check metadata registry (rlsbl/data/checks.toml), the
  check-to-target matrix in rlsbl/checks/__init__.py, and a row in
  docs/checks.md — the freshness tests fail otherwise.
- Every new command updates the strictcli schema dump, regenerated docs,
  and the wiring/effects/consequential pinning tests.
- Repo-wide sweeps follow the scripts/ sweep precedent: dry-run first,
  expected-occurrence assertion, full diff review.
- Authoritative test runs only on a quiescent tree.

---

## Phase 0 — Foundational groundwork

Effort: medium. No dependencies.

### 0.1 Releasable-layout and root-member test fixtures

The conversion test helpers in tests/test_releasable_extract.py build the
outdated per-package changelog layout, which is why the releasable-layout
blindness of the current commands is invisible to the suite. The shared
make_workspace fixture in tests/conftest.py never builds a root member and
never writes a releasables section.

- Extend the conversion test helpers to build explicit-mode workspaces with
  releasable state directories (version, changes, releases, config), based
  on the existing multi_releasable_monorepo fixture machinery.
- Extend make_workspace to support root members and releasables.
- Verify: at least one currently-passing conversion test is re-expressed on
  the new fixtures and passes; the fixtures are exercised by the suite.

### 0.2 Preview hygiene for conversions

The one bare `git status` in the tree (validate_absorb_preconditions in
rlsbl/commands/monorepo/extract.py) is not on the observe allowlist, so a
CLI-level absorb dry run truncates at the source-cleanliness probe. The
existing dry-run test calls the function directly and never sees it.

- Add `--no-optional-locks` at that call site, matching every other status
  invocation.
- Add a CLI-level regression test that drives `monorepo absorb --dry-run`
  through the app with a bound effects context.
- Audit the conversion modules for any other non-allowlisted observation.
- Verify: the new CLI-level dry-run test passes; the allowlist test suite
  stays green.

### 0.3 Shared plan/apply pattern

The mirror reconciler (rlsbl/commands/monorepo/mirror_cmd.py) is the only
observe/plan/apply implementation with per-state plan printing and
idempotent converge. Extract, absorb, and the publication reconciler all
need the same shape.

- Lift the reusable skeleton into a shared module (plan dataclass
  conventions, per-state plan renderer, observe-branch-apply entry point,
  the no-writes-above-this-line boundary convention), respecting the layer
  rules in rlsbl/layers.py.
- Migrate mirror_cmd onto it behavior-identically.
- Reconcile the duplicated is_ancestor helper with rlsbl/git_util.
- Verify: the entire mirror test module passes unchanged.

### 0.4 Lineage fact records

- Define the committed lineage record: an append-style JSON document in the
  releasable state directory recording conversion events (absorbed-from and
  extracted-to identities, old-to-new tag maps, departed tag globs,
  identity transitions), following the undo-audit write pattern
  (rlsbl/evidence_gate.py write_undo_audit) and validated the same way
  other rlsbl documents are.
- Provide read/write APIs and make the check context able to reach them.
- Add any new config keys to the config schema validator
  (rlsbl/config.py validate_config_schema) so the config-schema check
  accepts them.
- Verify: unit tests for write, append, read, and validation; a malformed
  record is a hard error.

---

## Phase 1 — Correctness fixes in code that survives the campaign

Effort: medium. Depends on phase 0 fixtures only.

Defects inside extract/absorb are deliberately deferred to phase 6, which
rebuilds that code; fixing it twice is waste. This phase fixes what the
rebuild keeps.

### 1.1 migrate-releasable defect set (red-green each)

- Preserve entry identifiers through consolidation, versioned-file merge,
  and dedup (rlsbl/releasable_migration.py consolidate_changelogs,
  _merge_versioned_files, _dedup_entries currently drop them, breaking
  amend/edit by id).
- Fix the truncation case: a releasable with unreleased entries and no
  member entries must not have its unreleased file overwritten empty.
- Resolve member tag globs through the shared resolver
  (rlsbl/tag_glob.py resolve_monorepo_tag_glob) instead of the hardcoded
  name-prefixed glob, so Go members are found.
- Fix the consolidation/migration tag interaction so the reported commit
  always matches the tag's actual target.
- Make cmd_migrate_releasable commit its own output (it currently leaves
  the tree dirty; the standalone cleanup entry point already commits).
- Scope cleanup to the target releasable's members, not the whole
  workspace.
- Verify: each fix has a failing-then-passing test; the migrate test
  modules pass.

### 1.2 Check-layer fixes

- workspace-unregistered becomes target-path-aware: a directory that is a
  declared target path of a registered member or releasable is exempt
  (enables deleting phantom members registered only to silence the check).
- Delete the pgdesign target's hardcoded schema-subdirectory detection
  fallback; pgdesign targets in subdirectories must be declared with an
  explicit path. Hard error where the fallback used to silently apply,
  with a message naming the explicit-path fix.
- The Go companion-tag check calls the target's companion_tags method
  instead of hardcoding the target name and re-deriving the format inline.
- Verify: red-green tests for all three; the feature-matrix and docs-table
  freshness tests updated where check metadata changed.

---

## Phase 2 — Target support matrix

Effort: large. Independent of phases 1 and 3; done early so later phases
build on the single idiom.

### 2.1 Protocol migration of behavior-encoded axes

Move every per-target support fact onto the target protocol or a
registry-derived structure, replacing name-keyed if/elif chains and inline
sets. Known sites: the yank dispatch (rlsbl/commands/yank.py), the test
runner's target chain with its silent success for unknown targets
(rlsbl/testing.py), the name-availability registries and chains
(rlsbl/registry.py, rlsbl/commands/check.py, claim-name), the
name-normalization dict (rlsbl/checks/project.py), the check skip-sets in
rlsbl/checks/quality.py, and the hand-typed other-target manifest list in
the plain target (derive from the manifest union in rlsbl/checks/__init__).

- Each feature declares one explicit unsupported-target policy; the test
  runner's silent pass for unknown targets becomes an explicit skip naming
  the target and reason, surfaced in the release step summary rather than
  buried in verbose output (the candidate's CI verification remains the
  test net for runnerless ecosystems).
- Verify: a new test asserts no target-name literal appears in a
  feature-support conditional outside the targets package (or an
  equivalent structural guard); all existing behavior tests pass with
  identical outcomes for supported targets and explicit outcomes for
  unsupported ones.

### 2.2 Capability derivation

- Delete the capabilities class attribute; derive each capability by
  introspection (method overridden versus base, template mappings
  present), with one derivation rule per axis in rlsbl/targets/introspect.
- Correct the false documentation claims (the pipelines doc's nonexistent
  capabilities; the targets doc's claims about checks consulting
  capabilities) as part of the derivation switch.
- Verify: the valid-capabilities test is replaced by derivation tests; the
  docs tables regenerate from derived data.

### 2.3 The committed matrix artifact

- Generate rlsbl/data/target-matrix.json from the registry covering every
  support axis from 2.1 and 2.2; commit via rlsbl commit.
- Add a freshness check (regenerate-and-compare, the snapshot-check shape)
  registered through the four-place convention.
- Extend the import-time completeness assertion so a new target cannot be
  added without answering every axis, and a new axis cannot be added
  without covering every target.
- Verify: the freshness check fails when a target's support changes without
  regenerating, and passes after regeneration.

### 2.4 Docs derivation

- Replace the hand-written per-target tables (targets doc capability and
  detection tables, dev-workflow install tables) with directives fed by
  the matrix; delete the duplicates.
- Fix the wrong hand-typed target counts by switching them to the derived
  count directive; make remaining prose counts-free.
- Verify: docs build cleanly; the dev-install help freshness test extends
  to the new derived tables where applicable.

---

## Phase 3 — Workspace ownership model

Effort: extra large. Depends on phase 0. Required by phases 6 and 9.

The root-attribution defect and watch deletion are one atomic change:
today a root member matches nothing by path prefix (the computed prefix
"./" never matches git-reported paths), so every existing root member owns
files only through watch globs — removing watch without fixing attribution
first would zero their territory.

### 3.1 The single-owner attribution core

- Introduce one ownership resolver: given the full member list and a file
  path, return the single owner (most specific member path wins; the root
  member owns the remainder minus the derived tool-owned exempt set).
  Fix the root path-prefix computation.
- Rewrite git_util's file_matches_project, filter_commits_for_project,
  filter_commits_for_releasable, and affected_projects around it; thread
  the full member list through every caller (changelog validate's scope
  filter, the changelog and prepush checks via the changelog-context
  helper in checks/_common, status, unreleased, changelog add's scope
  check and package derivation, monorepo status coverage, batch release
  init counting, releasable migration package derivation, the workspace
  test-suite check).
- Collapse the duplicate longest-prefix mapper in monorepo impact onto the
  resolver.
- Replace the fail-open path: a commit whose files cannot be determined is
  a hard error naming the commit and the git operation.
- Verify: property tests for the resolver (every file exactly one owner;
  most-specific wins; root gets the remainder); the impact, coverage,
  prepush, and status suites pass on the new semantics; a test proves an
  unattributable commit hard-errors.

### 3.2 Mandatory root member and reserved identity

- load_workspace validates: every workspace has exactly one member with
  path "."; its name is the reserved literal `root` (auto-applied when
  omitted; any other explicit name on it, or `root` on a non-root member,
  is a hard error). monorepo init scaffolds the root stanza; monorepo add
  handles adding "." to pre-existing workspaces during migration.
- Resolve the collisions with existing checks: a root member without a
  detectable target is exempt from the stale-entries and targets checks
  exactly as dev nodes are; the derived job-key, filter-key, and check
  regex forms for the reserved name adopt the existing root-publisher
  derivation in publish_inline as the single owned convention.
- Verify: a workspace without a root member hard-errors with the stanza in
  the message; the name rules are covered positively and negatively; init
  output contains the stanza.

### 3.3 Root-directory command resolution reclassification

- resolve_project returns the root member at the workspace root. Audit
  every call site that treats empty resolution as workspace-root
  detection and reclassify: project-scoped commands (changelog add,
  release run, status, unreleased, yank, deprecate, edit, undo, retry,
  init, scaffold) operate on the root member; workspace-scoped paths
  (check context construction iterating all releasables, monorepo
  commands, the CI-filter builders) detect the workspace explicitly via
  find_workspace_root.
- Verify: check at the workspace root still iterates every releasable;
  release run at the root releases the root member's releasable in a
  single-releasable fixture and errors for a dev-node root; each
  reclassified site has a test.

### 3.4 watch deletion

- Remove the key from the workspace types, loader, add command and flag,
  list output, and router generation. A workspace file still carrying
  watch is a load-time hard error with a migration message.
- Rewrite every user-facing error and remediation text that names watch
  (changelog add scope errors, CI-check remediation, release
  empty-window remediation, docstrings feeding generated docs).
- Verify: loader test for the hard error; a grep-style test asserts the
  key name survives only in the migration error message and historical
  docs.

### 3.5 Trigger derivation and router filters

- Router filters become derived: a member's filter is its owned territory,
  plus the territories of every member it declares dependencies on (all
  scopes; monorepo sync gains a WorkspaceGraph construction and its new
  failure modes are surfaced, not swallowed), plus built-ins — workspace
  root manifests and lockfiles trigger every member, the release-machinery
  auto-append generalizes the existing finalize-artifact append, and a
  router change re-runs everything — plus negated excludes for other
  members' territories. The root member's pattern is the match-everything
  glob plus excludes, replacing the broken "./**" form.
- The release-time filter simulation and the generator share one matcher
  that understands negation (the current simulator has none, so the
  release guard would otherwise disagree with real CI).
- Add a router-freshness check: the committed router's filters block must
  match regeneration (nothing polices this today; only job keys are
  checked).
- Verify: generator and simulator agree on a shared test corpus including
  negation; the freshness check goes red on a stale filters block; the
  batch-release contract tests (every member's filter matched by the
  candidate push) pass.

### 3.6 Root releasable tag format

- The Releasable tag-format field becomes an explicit-or-absent sentinel;
  the loader hard-errors when a releasable containing the root member has
  no explicit tag_format, with a message teaching the bare-version
  continuity choice; round-trip serialization preserves explicit values.
- Audit consumers of the parsed tag scheme for the bare-version scheme
  appearing inside a workspace (status, coverage anchoring, rename, the
  mixed-scheme guard for a Go root member).
- Verify: loader tests for the hard error and for non-root releasables
  keeping the default; a fixture releasable with a root member and
  bare-version tags round-trips through status and coverage.

### 3.7 Derived tool-owned exempt set

- One enumeration function for tool-owned paths, drawing on the existing
  changelog exemption path rules and the scaffold's managed-files
  knowledge; the ownership resolver consults it; document what it covers.
- Verify: unit tests; workspace-machinery commits remain coverage-exempt
  under the new model.

### 3.8 Test overhaul

- Replace the two vacuous root-member coverage tests with real positive
  and negative assertions (an uncovered root-owned commit fails).
- Rewrite the watch-pinning tests onto the new model; update the
  attribution-primitive tests, the router contract tests, and the fixture
  helpers.
- Verify: full suite green on a quiescent tree.

---

## Phase 4 — Release ledger anchors

Effort: large. Independent of phase 3. Required by phases 6, 8, 9.

### 4.1 Anchored archives

- Add optional released-commit and tree-hash fields to the release-file
  document schema (.strictspec/release-file.schema.toml) and regenerate
  the validator; bind the fields in the release-file reader.
- The finalization step authors the anchor into the archived file before
  it is locked read-only (the standalone path currently renames the
  operator's file verbatim — this adds the one authorship step); the batch
  synthesize path gains the fields directly. The anchored commit is the
  CI-verified candidate.
- release undo's unfinalize path round-trips anchored archives.
- Verify: schema validator regenerated and freshness-checked; a released
  fixture's archive carries the anchor; undo restores and re-finalizes an
  anchored archive.

### 4.2 The backfill script

- A repo script (following the existing backfill_changelog.py precedent:
  a one-time migration ships as a script under scripts/, never a CLI
  command) derives anchors for every archived version from existing tags
  across all recognized tag schemes, unlocking and relocking read-only
  files via the established writable pattern, stamping the missing
  format-version marker on pre-gate archives, and materializing missing
  archives from recoverable sources (existing CHANGELOG content, GitHub
  Release notes). Versions with no recoverable description, and versions
  whose tags cannot be found under any recognized scheme, are listed in a
  hard error for operator input — nothing fabricated, nothing skipped
  silently. Dry-run mode first with per-version reporting, per the batch
  discipline; runs are idempotent.
- The missing-anchor hard error introduced by the consumer switch (4.3)
  names this script as its remedy, and the docs describe running it from
  the rlsbl repo exactly as the changelog backfill docs already do.
- Verify: the script on a fixture with pre-gate archives, a missing
  archive, and an old-scheme tag produces anchors, a stamped gate, and
  the two hard-error lists respectively; a second run changes nothing.

### 4.3 The consumer switch

- Every internal version/range anchor reads the ledger: the last-tag
  primitive and its callers (status, unreleased, the coverage-range
  chokepoint in changelog resolve), undo's commit-walk (replacing
  message-shape classification), the two tag-list dialects in batch
  release init and monorepo status, the unscoped describe display line,
  and the destroyed-tag guard (fixing its documented dry-run false
  positive, since a ledger read has no preview-carrier degradation). Tags
  become a cross-check that hard-errors on disagreement with the ledger.
- Verify: each consumer has a test on a fixture where tag state and ledger
  agree (passes) and disagree (hard error naming both); the dry-run
  false-positive reproduction from the existing todo passes.

### 4.4 The unpublished-refs standing check

- A new check renders every ledger-known version to its full expected-ref
  set (primary tag, companion tags, boundary aliases) and compares against
  local tags and the remote: missing-locally, missing-remotely, and
  present-at-a-different-commit are three distinct hard errors; an
  inconclusive remote probe is also a hard error. Network-tagged.
- Verify: four-way fixture coverage; registered through the four-place
  convention.

---

## Phase 5 — Composable rewrite commands

Effort: medium. Depends on phase 2 (target idiom). Required by phases 6
and 9.

### 5.1 Go module-path rewrite

- `rlsbl rewrite go-module-path`, in a new rewrite command group (the
  home for this family as it grows): a standalone plan/apply command
  rewriting the module line (single reader
  exists in utils; the writer is new) and sweeping every import site via
  the existing tree-sitter Go import scanner and the equality-or-prefix
  match rule from the import scanners. Plan reports per-file occurrence
  counts; apply asserts the expected counts; usable for plain repo renames
  independent of conversions.
- Verify: fixture repo with imports across packages rewrites completely;
  count mismatch aborts; plan output lists every file.

### 5.2 uv path-source rewrite

- `rlsbl rewrite uv-path-sources`: generalize the existing pypi
  build-time dependency rewriter into a
  working-tree plan/apply command covering project dependencies, optional
  dependencies, dependency groups, and tool-uv-sources entries; floors at
  the locked version; a floor version absent from the registry is a hard
  error naming the release-first remedy; updates the internal
  dependency-floor config list.
- Verify: fixture with a workspace source and a path source converts to
  floors; the unpublished case hard-errors; the floors check passes after
  conversion.

---

## Phase 6 — Conversion rebuild as plan/apply

Effort: extra large. Depends on phases 0, 3, 4, 5.

### 6.1 Extract: verified move-out on releasables

- One extract command operating on releasables (the package-level command
  and the separate releasable variant collapse; extracting a package from
  a shared releasable directs the operator to split the releasable first).
  Plan: previews tag translations and deletions, collisions, the tree
  verification, inbound dependency refusals with exact rewrite-command
  invocations, the lineage record, and the follow-up hints.
- Apply: clean-tree precondition; clone and filter; tree-object identity
  verification between the source path and the new repo before any state
  transform; full state transplant (changes, version file, release
  archives, config — reusing the migration module's copy helpers and the
  locked-file write pattern); tag translation via the kept classifier with
  the boundary tag and lineage record; then the source side — saferm
  deletion under the `--delete-with-rm` consent contract, workspace edit
  committed,
  router re-synced, snapshot regenerated, departed tag globs recorded.
  The repo lock is held. Inbound local-dependency edges refuse before any
  mutation; the departing name joins the floor list.
- Reclassify extract as consequential; update the consequential pinning
  test and the effects/wiring pins; correct the README's consequential
  command list via the template.
- Verify: round-trip (absorb then extract) coherence test on
  releasable-layout fixtures; tree-verification failure aborts with both
  hashes; deletion-consent matrix (saferm present, absent-with-flag,
  absent-without-flag); dependent-edge refusal names the phase-5 command;
  source workspace passes all workspace checks immediately after apply.

### 6.2 Absorb: releasable-only with healing

- Absorb targets a releasable, auto-creating a singleton whose tag format
  is written explicitly (derived from the member's primary target scheme);
  implicit-mode workspaces are refused with a pointer to
  migrate-releasable. Validation reaches parity with monorepo add
  (releasable existence, dependency-name existence, remote host
  consistency) and runs scaffold and sync in apply as add does.
- The working clone lives under the monorepo's .git/rlsbl/ directory and
  is announced in the plan. Tag import is uniform with a pre-checked
  collision policy (ref-name and same-version are plan-time hard errors)
  plus one boundary alias; the fetched bare tags are never deleted from
  under pre-existing monorepo tags. The hash-remap report is surfaced;
  release archives are migrated in releasable mode; versioned changelog
  files are written locked; the silent same-version merge skip becomes an
  error. Crash recovery is idempotent re-run healing. The repo lock is
  held; the snapshot regenerates; completion output prints next-step
  hints including the Trusted Publisher settings path where a PyPI target
  moved.
- Two standing hard errors in dev sync and dev status: an overlay whose
  package is a member of the current workspace, and an overlay whose path
  resolves inside the repo — each naming the one-line remediation.
- Verify: healing test (kill after merge, re-run completes without
  double-merge); collision fixtures; releasable-layout absorb migrates
  archives and surfaces the remap report; the overlay errors fire on
  contrived local-only files; workspace checks pass immediately after
  apply.

### 6.3 Conversion docs

- The narrative conversion chapter in docs/monorepo.md (semantics, tag
  policy, verification, the follow-up model); regenerated CLI docs; the
  stale generated API page for the extract module removed (the monorepo
  commands package stays excluded from API generation; the underlying
  stale-output problem is filed as a todo in selfdoc).
- Verify: docs build; the docs freshness tests pass.

---

## Phase 7 — Standing checks from facts

Effort: medium. Depends on phases 0 (facts), 4 (ledger), 6 (writers).

- Go module identity: the module path in go.mod must equal the origin
  remote's repository identity plus the member subdirectory (catches
  conversions, renames, and hand-moved modules alike).
- Lockfile-manifest consistency: the dep-locks check (sibling of
  dep-floors, which it structurally mirrors) — locks resolve the current
  manifests.
- npm token presence on the repo via the GitHub API (a pinned observe
  allowlist entry for listing secret names is required).
- Old-repo-archived and old-Go-module-deprecation-published: derived from
  lineage records plus the GitHub API and the module proxy's module-file
  content respectively, with the established request-backoff helper and
  timeouts.
- All network-dependent checks carry the network flag and preflight-class
  tags; all four-place registrations; per-check fixtures for pass, fail,
  and inconclusive.

---

## Phase 8 — Publication reconciler

Effort: large. Depends on phase 4; benefits from phase 6's lineage.

- A ledger-driven planner alongside the existing journal-driven reconcile:
  observe via the existing remote-ref snapshot plus a single GitHub
  Release listing; per-version verdicts (materialize, already-correct,
  refuse-foreign, refuse-identity-mismatch) computed against the full
  expected-ref set; leased pushes and Release recreation reusing the
  existing reconcile helpers; the tripwire hard-errors on any ref the
  ledger and lineage records cannot explain, touching nothing.
- Consent is file-driven: plan writes a reviewable plan file; apply reads
  it; the command is consequential. No per-item prompting mechanism is
  invented.
- Per-target materialization policy on the target protocol; the Go policy
  refuses any version whose recorded module path differs from the current
  one. The published-evidence guard gains a module-proxy evidence source
  for Go (the current Go probe is circular for tag adjudication — it
  checks the very tag being adjudicated).
- The never-push rule's restatement (branch heads: releases only; tags and
  Releases: the reconciler and the release tag step) lands in the CLAUDE
  and README templates and the release-workflow doc.
- Verify: verdict fixtures for all four classes; the tripwire fixture
  (unexplained ref aborts everything); a Go identity-mismatch fixture is
  never materialized; plan-file round-trip; docs freshness tests.

---

## Phase 9 — Release mirrors

Effort: large. Depends on phases 3 (workspace schema), 5 (Go rewrite),
6 (extract), 8 (policy shape).

- The mirror destination moves to a releasable-level field (loader,
  serialization, validation: a multi-member releasable with a mirror is a
  hard error); the per-project subtree-remote field migrates with a
  load-time error naming the move.
- The release's mirror step is rebuilt: invoke the reconciler's converge
  (leased; the only writer of the mirror branch), then tag the converged
  tip and push only the tag; the existing unforced main-branch push is
  deleted (it is rejected by any converged mirror today, failing releases
  after the tag push). The step markers keep their trivially-done
  completeness contract.
- The mirror scaffold commit gains the Go module-path rewrite to the
  mirror identity (using the phase-5 rewriter); go.mod joins the
  scaffold-owned path set so the tripwire tolerates it; mirror scaffolds
  drop the publish workflow.
- The mirror reconciler's plan gains a tags dimension: missing release
  tags on the mirror are a heal-able state, materialized from the ledger.
- A registration-time check: a Swift member in a workspace requires a
  configured mirror.
- Mirror promotion: extracting a mirrored releasable adopts the mirror's
  history — the subtree-split correspondence map is regenerated on demand
  (the split is deterministic; the cache is disposable) and persisted
  alongside the lineage record, feeding the existing changelog remap;
  the monorepo side is then retired through the normal extract source
  path.
- Verify: the mirror test module's state coverage extends to the tags
  dimension; a release against a converged mirror fixture pushes exactly
  one tag and leaves the branch untouched; a promoted mirror fixture's
  changelog hashes all resolve; the Swift check fixture.

---

## Phase 10 — Documentation sweep and the release

Effort: small-medium. Depends on everything.

- Remaining docs: mirror chapter corrections, root-member and tag-format
  reference, the workspace field table made counts-free, regenerated CLI
  and schema surfaces.
- A quiescent-tree full verification: entire suite, all checks, docs
  build.
- Changelog entries for the campaign (user-facing per discipline), the
  release file with description and context, and the single release.
- After the release: the fleet repos' filed todos become actionable
  against the published version (out of scope here).

## Deliberately not in this campaign

- The portable-release-identity end state (release records as the sole
  identity with tags as pure projections) was considered and not adopted;
  the ledger-plus-reconciler architecture above is the ratified stopping
  point.
- Fleet workspace normalization (root stanzas, conformance members, watch
  removal per repo) lives in the affected repos' own todos.
- selfdoc-side work (member-scoped docs transport, directive-failure
  hardening) lives in the selfdoc repo's todos.
