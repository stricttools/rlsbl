# Campaign plan: version-fate model, strictness, truthful surfaces, backfill promotion, and fleet repair

Implementation plan for a single campaign across rlsbl and the fleet. Every
design decision is made and approved by the user; no phase contains open
design work. Grounded against the working tree at rlsbl 0.119.0 and against
the fleet repos by direct probes; critiqued by an independent four-area
review whose blockers are folded in.

## Terminology (all of it defined here; the phases use these terms freely)

- **The release archives**: the per-version TOML files under
  `.rlsbl/releases/` (per releasable under
  `.rlsbl-monorepo/releasables/<name>/releases/` in monorepos). The
  authoritative record of versions. An archive may carry an anchor
  (`candidate_sha` + `tree_hashes`: the commit and trees that shipped), or
  `unanchorable = true` (the shipping commit is unrecoverable), or — new in
  this campaign — `never_released = true` (the version number exists but was
  never a release).
- **The shipped-as field**: a new optional archive field recording the tag
  spelling a version actually shipped under, for versions released before a
  releasable rename (e.g. a version whose real tag is `strictcli@v0.12.0`
  while the current scheme is `py-strictcli@v{version}`).
- **The transition record**: the committed append-style store of
  repository-level events, today named `lineage.jsonl` and renamed by this
  campaign to `transitions.jsonl` (command surface: `rlsbl transition
  record`). It holds rewrites, renames, boundary moves (absorb/extract),
  mirror-promotion commit maps, and — new — the two kinds
  `release-history-closed` and `non-version-tag`.
- **The fate taxonomy (closed)**: every version number or tag the world has
  seen is exactly one of: released; never-released; deliberate non-release
  tag; release-history-closed; non-version tag. Placement rule: a fact about
  ONE VERSION lives in that version's archive (released is the default
  meaning of an archive; never-released is an archive marker; a deliberate
  version-shaped tag is explained by its never-released archive, needing no
  separate record); an event about the REPOSITORY lives in the transition
  record (release-history-closed, non-version tags, and the pre-existing
  event kinds). Anything unclassifiable forces a deliberate taxonomy
  extension, never a quiet wedge.
- **Adopt-as-released**: the ruling that a version-shaped tag published on
  origin that no store records was a release; it gets an archive anchored
  from its tag, with description recovered (see recovery chain) and bump
  derived by version arithmetic, every reconstructed field marked with its
  source.
- **The identity ruling (minting)**: a renamed releasable's past versions get
  current-spelling tags minted at their recorded commits through the
  reconcile repair surface; the old-spelling tags remain in place, explained
  by their archives' shipped-as fields; once minted, a version's expected
  primary ref is the current spelling.
- **The recovery chain** (for reconstructed archive descriptions), in order:
  GitHub Release body (bullet-aware — old bodies open with headings and
  bullets, not lead paragraphs) → the version's CHANGELOG.md section → commit
  subjects in the version's tag range → placeholder. Every recovered value
  names its source in the archive.
- **The pre-approved limits** for unattended outward writes, approved by the
  user with the marker [%%] (trust-adopted, freely reversible, never to be
  cited as deliberate intent):
  - Auto-apply is permitted only for: creating a GitHub Release on an
    archived version's existing tag; creating and pushing a tag at an
    archive's recorded commit under the version's expected spelling; pushing
    an archived tag origin lacks.
  - Only materialize / already-correct verdicts qualify. Deletions, moves of
    existing refs, force-pushes outside mirror flows, and any repair or
    refusal verdict never auto-apply.
  - Ceilings: 130 GitHub Release creations and 80 tag creations fleet-wide
    (approved at measurements of roughly 100 and 62; the measurement
    re-derivation in phase 0.6 re-checks them, and a material excess pauses
    for the user's reconfirmation).
  - Apply mechanic: before each apply, the written reconcile plan file is
    inspected item by item; every item must match an approved write kind and
    verdict or that repo's whole plan pauses (reconcile has no selective
    apply). The operator loop keeps the running fleet-wide tally against the
    ceilings and records it in the close-out report.
  - Exactly two user-ordered exceptions, enumerated here and nowhere else:
    (1) a private project's milestone tags (the `phase-*-complete` /
    `audit-program-complete` family) are DELETED locally and on origin;
    (2) pgdesign's v1.0.1 retraction tag is PUSHED. Both are performed as
    attended steps in their phases via plain git, outside the auto-apply
    machinery, on the user's explicit standing order recorded here.
- **Consent pauses**: unattended execution stops exactly at (a) an
  outward-writing plan whose items fall outside the pre-approved limits, and
  (b) a disposition of history that no record and no approved class ruling
  explains. Nothing else pauses.

## Standing conventions (binding on every phase)

- Implement+audit pairs; auditors are blind (spec plus tree, no git
  history, no implementation notes).
- Every Verify clause below is implemented red-first. The phase 0.4 fixture
  list is only the cross-phase shared set, not the exhaustive one.
- Every error message that names a remedy gets a test that performs the
  remedy verbatim and asserts the error clears.
- One safegit commit per work-item with its changelog entry; machine-
  generated files via `rlsbl commit`.
- Relevant suites green at phase boundaries; sweeps dry-run-first with
  expected-occurrence assertions and full diff review.
- Every phase — code and fleet alike — ends with a falsified-text sweep of
  the text its own changes made stale, in every repo it touched.
- Any archive or record field written by tooling after ship time carries an
  explicit reconstructed/derived note naming its source.
- Rulings that rest on measurements are recorded with them; a materially
  moved measurement pauses for the user's reconfirmation before acting on
  the old ruling.
- One rlsbl release, at the end of the code phases (phase 6). Never push
  outside sanctioned flows; the two enumerated exceptions above are the only
  hand-performed ref writes in the campaign.
- Subprocess helpers take explicit timeouts (fix on touch where absent).
- New checks register in every registration place the codebase requires;
  new commands update the schema dump, regenerated docs, and the
  wiring/effects/consequential pinning tests.

## Decision origin

All rulings in this plan are the user's deliberate decisions, except these
trust-adopted elements (freely reversible, never to be cited as deliberate
intent): the pre-approved limits [%%]; the reconstructed-description format
(joined bullet text marked with its source); the placement of the shipped-as
field in the archives rather than the transition record (adopted from a
design discussion the user endorsed).

## Phase dependency notes

Phases run in numeric order. Within that: 0.5 (the rename) precedes all code
phases because it touches the same files phases 1-4 edit most; 1.1 precedes
any archive carrying new keys (the validator rejects unknown keys); 2.3
(minting capability) precedes phase 10's reliance on it; phase 6 (the
release plus install update) precedes every fleet phase; phase 9's
recordings precede phase 10's reconcile pass; phase 11's newly anchored
repos get their own reconcile within phase 11.

---

## Phase 0 — Foundations

Effort: medium.

### 0.1 Pin and toolchain

- Replace the machine-wide editable install with released rlsbl 0.119.0;
  commit the restore-command record (no foreign paths in it).
- Install the current strictspec CLI. Measured at planning: the installed Go
  CLI self-reports 0.2.1 while the Python runtime and the generated
  validators are 0.2.3; regenerating validators with the stale CLI stamps
  the older version into the generated files and every import then
  hard-errors against the newer runtime.
- Verify: pinned rlsbl version reported; strictspec CLI at or above the
  runtime version; restore record committed.

### 0.2 Fleet alias pre-edit

- The legacy `dev_node = true` member key is in live use in these repos
  (measured at planning; re-grep before editing): strictcli, orxtra (two
  members), WWW, gamehome (two members), mobileinfra, incantino.
- Pre-edit EVERY use in the four repos the current loader reads — strictcli,
  orxtra, WWW, gamehome — replacing each with the two-key form
  (`dev_only = true` plus `releasable = false` where absent; a bare rename
  is not equivalent where the second key is missing).
- mobileinfra and incantino are deliberately excluded: they run pinned
  0.117.2, which predates the `dev_only` spelling; their implicit-mode
  conversions (their own filed todos) own that transition, and phase 3.2's
  error message names the full two-key remedy for when they arrive.
- Verify: the four edited repos' workspace files load under current rlsbl
  (the three currently load-refused repos are checked by confirming the
  alias error specifically is absent from their loader output); grep
  confirms no `dev_node` remains in the four.

### 0.3 External todo filings

- selfdoc: (a) the nested-subgroup rendering gap — its group-page renderer
  iterates only a group's direct commands and never its nested groups, so a
  subgroup's commands appear on no page; (b) the inconsistent short-help
  measurement — it measures the choice help for some flags and the
  member-value help for others.
- strictspec: its runtime emits a migrate invocation its own CLI rejects
  (a flag spelling of a positional argument) and that cannot repair a
  document missing the format-version marker at all; the template is
  spec-pinned, so the fix is spec plus code there.
- strictcli: propose a framework-level supplied-but-empty flag stance, so
  per-consumer empty-string refusals eventually collapse into the framework.
- Verify: todos committed in all three repos.

### 0.4 Shared defect fixtures

- The cross-phase red set: a cross-filed changelog entry (commit in another
  releasable's territory) misreported as out-of-range; a phantom version
  anchored from its version-bump commit; a pre-marker archive missing
  required fields that the current backfill leaves invalid; unknown keys on
  each workspace configuration surface; an explicitly-empty flag value
  silently dropped.
- Verify: each fixture demonstrably red against current behavior.

### 0.5 The vocabulary renames (moved first, before the code phases)

- Sweep the old release-record word ("ledger") out of rlsbl prose, error
  texts, and identifiers in favor of "the release archives" / "the release
  record"; sweep "lineage" to "transition record" (`transitions.jsonl`,
  module and identifier renames to match). Rename the committed
  `lineage.jsonl` files in the fleet repos that have them (one commit each;
  no dual recognition anywhere — pre-stable, no compat).
- Rewrite the rlsbl reference sections of the user-level instruction file
  at `~/Projects/CLAUDE.md` to the new vocabulary.
- Historical changelogs and archived release files stay untouched as
  immutable record.
- Verify: grep for both old words is clean outside immutable history;
  selfdoc baselines re-accepted after review; the fleet files renamed.

### 0.6 Fleet measurement re-derivation

- The critique found the planning-time counts stale (22 standalone
  rlsbl-managed repos, not 17; roughly 100 version tags without archives
  across seven repos, not the earlier partition). Re-derive, per repo: the
  materialize-from-changelog count, the adopt-from-tags count, the
  format-stamp count, and the projected outward writes (Release creations
  and tag creations).
- Record the table; confirm the approved ceilings (130/80) still bound the
  projection, or pause for reconfirmation per the measurements convention.
- Verify: the written per-repo table exists in the campaign's working notes
  and the ceilings are confirmed or escalated.

## Phase 1 — The version-fate model

Effort: large.

### 1.1 Schema and binding

- Add TWO optional keys to the release-file schema in one widening edit
  (no format-version bump obligated; verified against the schema authority's
  own bump rule): `never_released` and the shipped-as field.
- Regenerate the validator with the aligned toolchain; bind both fields in
  the archive reader; the editable release file refuses both exactly as it
  refuses the anchor fields (flow-owned).
- Verify: an archive carrying each key validates; the editable-file refusal
  fires for each; generated files carry the current strictspec stamp.

### 1.2 Read-path semantics for never-released

- Excluded from: the latest-release fact (the next real version wins); the
  unreleased-range computation; undo's latest-version pick (which today
  selects a phantom-topped repo's phantom and dies); ref-presence
  expectations (counted, never demanded); reconcile verdicts (skipped and
  counted; a still-existing tag of such a version is reported as explained,
  never deleted); the contains-latest refusal.
- Changelog generation renders a never-released version's section explicitly
  annotated as never released, from the archive's own description (a phantom
  can have real finalized changelog files; hiding the section would lose
  record).
- Status renders a distinct label; the JSON payload gains fields expressing
  both non-default archive states (it currently collapses them to null).
- Verify: per-consumer fixtures including generation's annotated section;
  undo on a phantom-topped fixture undoes the real latest.

### 1.3 Loud archive reads in changelog generation

- The raw-parse path that silently turns an unreadable archive into an empty
  description becomes a hard error through the real reader.
- Verify: a schema-invalid archive aborts generation loudly.

### 1.4 Transition-record kinds

- The transition-record schema gains the two new kinds
  (`release-history-closed`, `non-version-tag`), with the same
  schema-edit-plus-validator-regeneration obligation as 1.1 (these records
  are validated documents too).
- Named consumers: the backfill (recorded non-version tags leave the foreign
  listing; a closed history changes nothing there), the residue check
  (phase 5.5 exempts a member whose history is recorded closed), and
  reconcile (a recorded non-version tag is an explained ref).
- Verify: malformed records refused; each kind round-trips; regenerated
  validator stamped current.

## Phase 2 — The commands

Effort: extra large.

### 2.1 `rlsbl release backfill` (promoting the script)

- Why promotion: a shipped hard error (the empty-record-in-tagged-repository
  refusal) names the script invocation as its remedy, which an installed
  user cannot follow; and any external adopter with pre-existing release
  history needs this operation at onboarding.
- Built on the preview/apply skeleton; mutating; consequential with its
  inline justification; framework dry-run; one command run per repo.
- Behavior — everything the current script does, plus:
  - Existing archives completed: ALL missing required fields recovered
    (description via the recovery chain, targets-derived include, default
    exclude), not only the format-version stamp.
  - The recovery chain as defined in Terminology, bullet-aware, every
    recovered value marked with its source; the materialized-archive header
    comment updated to enumerate the new sources.
  - A per-version never-released declaration, so a phantom is never anchored
    from its bump commit again.
  - Adoption of scheme-matching version tags that no store records
    (adopt-as-released), anchored from their tags.
  - Consultation of shipped-as fields so renamed history anchors instead of
    counting as foreign.
  - Recorded non-version tags excluded from the foreign listing.
- Named reuse, not re-creation: the GitHub Release body reader is the one in
  the publication module (which exists to eliminate duplicate call sites of
  exactly that kind); tree-at-commit resolution consolidates onto ONE shared
  helper rather than adding another copy beside the existing ones (release
  flow, anchor remap, extract, absorb, the script); "is this tag explained?"
  is answered by ONE shared consultation function used by both this command
  and reconcile (archives for version-shaped tags, the transition record for
  the rest).
- Both old scripts retire as removal stubs: the anchor backfill script AND
  the archived-description rewriting script (its job is subsumed by the
  recovery machinery).
- The shipped remedy is retargeted at every live mention site (measured at
  planning: two in the archive-reading module, one in undo, one in the
  release-workflow doc, plus the user-level instruction file), and the
  hand-written multi-step recovery procedure in the missing-anchor error is
  replaced by naming the command.
- Verify: the 0.4 fixtures green; idempotent second runs plan nothing;
  remedy-followability tests drive each printed remedy verbatim; grep
  confirms no live reference to either retired script.

### 2.2 `rlsbl transition record`

- The typed door for operator-declared transition-record facts (the two new
  kinds), refusing unknown kinds; mutating; non-consequential with the
  stated reason (a local, validated, committed record edit); dry-run
  supported; `--auto-commit` as the standard opt-out boolean.
- Verify: each kind recordable; unknown kind refused; recorded facts change
  backfill, residue-check, and reconcile behavior as 1.4 names.

### 2.3 Minting capability

- Verify whether reconcile's materialize path can already create and push a
  tag absent both locally and on origin at an archive's recorded commit;
  build the capability if not.
- Implement the expected-spelling derivation for renamed history per the
  identity ruling: expected primary is the current spelling; the shipped-as
  field explains the old tag.
- Verify: a renamed-releasable fixture in which reconcile's plan mints the
  current-spelling tag at the recorded commit while the old tag stands
  explained; the tag is created only via apply, and the write matches the
  pre-approved write-kind list.

## Phase 3 — Deletions and strictness

Effort: large.

### 3.1 Delete the remote-release feature

- The dispatch workflow template, both scaffold call sites and their helper,
  and the feature's test file; immutable changelog mentions stay; the
  manifest regenerates.
- Verify: no live reference; scaffold output carries no dispatch workflow.

### 3.2 Unknown-key refusal (the strictness ruling's policed surfaces)

- The known key set for workspace member tables is DERIVED from the model in
  one place (the codebase's own derive-keys-from-the-model convention — not
  a hand-typed list). Refusal at load on: both workspace table kinds, the
  workspace file's top level, and the standalone releasable file.
- The `dev_node` alias is deleted outright: a hard error naming the two-key
  remedy (`dev_only = true` plus `releasable = false`). The four
  current-loader repos were pre-edited in 0.2; the two pinned repos meet the
  error via their own conversion todos.
- The standalone releasable file aligns to explicit-or-absent for its tag
  format (no default folding).
- The save path strips runtime-injected bookkeeping keys so they can never
  be persisted and then refused on the next load.
- This is the stated interim consistent with the existing schema-ownership
  todo; the eventual schema wiring is the strictness scope list's named
  later item (filed at close-out), and this change leaves nothing that item
  must undo.
- The enumerated tolerant-behavior tests flip to refusal tests (including
  the recent losslessness tests this supersedes).
- Verify: refusal fixtures per surface naming the offending key; all four
  migrated fleet workspaces still load.

### 3.3 Empty-flag refusal

- Every explicitly-empty string flag value that is today silently treated as
  absent becomes a hard error naming the flag — implemented centrally
  through the framework's supplied-or-not predicate rather than per-site
  checks, so the eventual framework-level stance (the strictcli todo from
  0.3) replaces one site. The inventory measured at planning spans the
  monorepo add/graph/impact/absorb/init commands, the batch-release-init
  filter (where an empty value silently selects every releasable), the dev
  include/exclude flags, and both target resolvers; re-grep at
  implementation time.
- File-key empty-means-unset conventions are untouched.
- Verify: per-flag refusal tests through the central path.

## Phase 4 — Truthful surfaces

Effort: medium.

### 4.1 Entry removal

- `changelog edit` gains a removal mode — selected by id or commits,
  exclusive with field updates via the framework's choice-flag convention,
  atomic rewrite, with the released-file unlock/regenerate flow — the
  capability two live error messages already claim exists.
- Verify: removal round-trips on unreleased and released fixtures; the
  printed remedy executes verbatim in a test.

### 4.2 Message corrections

- The range check distinguishes out-of-scope from out-of-range (the actual
  condition in the two blocked fleet repos, currently mislabeled).
- The orphans remedy names real invocations (the current one names a flag
  and a capability that do not exist).
- The name-availability command's usage line names the command's real name.
- Status's release hint names the full release-run invocation instead of the
  bare command group.
- Status at a workspace root without a root manifest errors naming the
  monorepo status command instead of a missing-manifest complaint.
- Verify: followability tests for each corrected message.

### 4.3 Help strings

- The four flag helps measured short at planning (the scrub mode trio and
  the module-rename target flag) are lengthened on BOTH the choice and
  member-value sides, so the doc generator's inconsistent measurement passes
  either way.
- Verify: those warnings gone from the doc check.

## Phase 5 — Hygiene

Effort: medium.

### 5.1 Dead and unused

- Both zero-caller ownership helpers deleted; unused parameters dropped; the
  stale "future Flutter target" comment corrected.

### 5.2 Resolution memoized

- The changelog-context resolution cached per check-run context (measured:
  nine-plus full re-resolutions per check invocation today, plus adjacent
  double-resolutions).
- Verify: a counter test pins one resolution per context.

### 5.3 Ruff over tests

- Lint scope extended to the test tree; auto-fixes applied; the
  assigned-unused findings reviewed semantically.
- Verify: repo-wide lint clean at the configured rules.

### 5.4 Flutter entry points

- The Flutter target overrides dead-module entry-point derivation for its
  main-file convention (today a Flutter app with no derivable entry silently
  analyzes nothing and reports clean).
- Verify: a Flutter-app fixture analyzes from its real entry point; the
  support matrix regenerated if any axis answer moves.

### 5.5 Residue-check extension

- Release state (archives, changes, scheme-matching tags) on a dev node or
  non-releasable member becomes a finding — unless the transition record
  marks that member's release history as closed.
- Verify: red fixture; a closed-history record silences it.

## Phase 6 — The release

Effort: medium.

- Blind audits covering phases 0.2 through 5 (the fleet pre-edit and the
  fixtures included), fix rounds as needed, then the single minor release;
  the pinned install updates to the new release.
- Verify: registries serving the new version; suite and checks green; the
  new version's archive anchored.

## Phase 7 — Standalone-fleet sweep

Effort: large. Runs on the released rlsbl via the new backfill command.

- Per repo across the measured sweep set from 0.6: dry-run reviewed, then
  apply and commit. Expected content per the measurements: materializations
  with recovered descriptions; early-tag adoptions (safegit and claudewheel
  recover real content from their own GitHub Release bodies; howmuchleft
  from commit subjects); tinymoon's tagless version anchored from its bump
  commit.
- a private project: FIRST the user-ordered exception — its milestone-tag
  family deleted locally and on origin (attended, plain git, per the
  standing order recorded in the limits section) — then its backfill.
- Verify: every guarded read (status, unreleased) works across the whole
  sweep set; second runs plan nothing; per-repo falsified-text sweep.

## Phase 8 — pgdesign (attended, single merged visit)

Effort: medium.

In this order:

1. The v1.0.0 archive rewritten from the unanchorable wedge to
   never-released (safe now: the installed rlsbl reads the key).
2. The dirty tree inspected and committed.
3. The self-inclusive retract directive added to go.mod
   (`retract [v1.0.0, v1.0.1]` with a comment stating v1.0.0 was an
   accidental tag, never a release), committed with its changelog entry.
4. The v1.0.1 archive written never-released with the retraction note —
   BEFORE any push, so the record precedes the ref.
5. The second user-ordered exception: the v1.0.1 tag created at that commit
   and pushed via plain git, attended.
6. The proxy poked to fetch v1.0.1; resolution verified flipped: the go
   tool's latest query answers the real highest 0.x, and both 1.x versions
   leave default version listings.
7. Text made true in the same phase: pgdesign's own rationale paragraph
   about the phantom, AND the user-level instruction file's claims that the
   phantom is unretractable and that retraction would require a banned tag —
   both falsified by this phase, both rewritten to the post-retraction
   reality (the no-1.x rule itself stands, with this recorded as its one
   granted exception).
8. The repo's one uncovered changelog commit covered.

- Verify: the go tool resolves latest to 0.27.x; both 1.x versions absent
  from default listings; all pgdesign checks green, including generation's
  annotated never-released sections.

## Phase 9 — Workspace-fleet repairs

Effort: large.

- stricttest and selfdoc: the cross-filed changelog entries removed via the
  new removal mode (their corrected error messages now print exactly that
  remedy); selfdoc's one uncovered commit covered.
- strictcli: the backfill re-anchors the wrongly-unrecoverable archives from
  their real old-spelling tags, writing each version's shipped-as field in
  the same unlock pass; ONE transition-record entry records the rename event
  itself (reason and scope stated once, not fragmented); the unmatched early
  tags adopted as released; the conformance member's release history
  recorded closed; the local-only tag left for phase 10.
- strictspec and stricttest: their small old-spelling tag sets handled the
  same way (shipped-as fields plus one rename event each).
- Verify: changelog and workspace check tags green in all four; no archive
  remains marked unrecoverable whose tag exists anywhere.

## Phase 10 — The reconcile pass

Effort: medium.

- Reconcile plans across every repo anchored AT THIS POINT (the phase 11
  repos are not yet anchored and are excluded here), auto-applied under the
  pre-approved limits with the stated apply mechanic: the minted
  current-spelling tags at recorded commits, the missing GitHub Releases,
  the archived-but-unpushed tags. Pauses only on a repair/refusal verdict, a
  non-qualifying write kind, or a ceiling breach.
- Verify: ref-presence checks green across the repos covered so far; the
  running tally within ceilings; a written summary of every ref created.

## Phase 11 — Root-kind rollout and close

Effort: large.

- orxtra via the migration script (tag format written on its releasable,
  watch keys deleted; its alias keys were fixed in 0.2), then its backfill.
- a private project: its dirty tree inspected and committed first, then the
  dev-node root edit via the script.
- WWW: the dev-node root edit; its two member-path tags get shipped-as
  handling under the releasable that owns those members.
- gamehome: deferred to its release hold. Its todo carries the two
  hand-steps the script does not perform: rewriting the dangling
  depends-on reference to the renamed root member, and the reckoning of its
  two version-tag families.
- Then, in order: reconcile runs for the repos newly anchored in this phase
  (same limits, same tally); the editable install is restored; the closing
  fleet pass runs (status plus cheap checks in every repo — by hand this
  once; the fleet health runner is another project's filed todo); the
  deferred items are filed as todos in this repo's backlog (the batch
  release-file reader's unification onto the schema authority; the
  config-schema completion and wiring, per the strictness scope list);
  completed todos move to done; and the final report reconciles every
  measured outcome against its recorded approval, including the final
  write tally against the ceilings.

## Deliberately not in this campaign

- The config-file unknown-key refusal (the schema completion and wiring):
  the strictness scope list's named later item, filed at close-out.
- The batch release-file reader's unification onto the schema authority:
  filed at close-out.
- The three implicit-mode workspace conversions (their own filed todos, on
  the pinned pre-campaign release, on their own schedule).
- gamehome's migration (blocked on its release hold; its todo carries the
  hand-steps).
- The fleet health runner (another project's filed todo).
- The framework-level empty-flag stance (strictcli's todo, filed in 0.3);
  rlsbl's central predicate use in 3.3 is the consumer-side interim.
