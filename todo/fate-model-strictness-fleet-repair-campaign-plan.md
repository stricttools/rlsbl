# Campaign plan: version-fate model, strictness, truthful surfaces, backfill promotion, and fleet repair

Implementation plan for a single campaign across rlsbl and the fleet. Every
design decision is made and approved by the user; no phase contains open
design work. Grounded against the working tree at rlsbl 0.119.0 and the
fleet repos by direct probes; hardened by a briefed four-area critique and
a blind four-area critique, both folded in. The campaign's running record
(measurements, tallies, per-phase outcomes) lives in ONE companion file:
`todo/fate-model-campaign-record.md`, created in phase 0 and moved to done
with this plan at close.

## Terminology (defined here; the phases use these terms freely)

- **The release archives**: the per-version TOML files under
  `.rlsbl/releases/` (per releasable under
  `.rlsbl-monorepo/releasables/<name>/releases/` in monorepos). The
  authoritative record of versions. After this campaign an archive is in
  exactly ONE of three states: **commit-recorded** (`candidate_sha` +
  `tree_hashes`: the commit and trees the version shipped from),
  **commit-unrecoverable** (a real release whose shipping commit cannot be
  identified; the marker key is renamed by this campaign from its old
  spelling `unanchorable` to `commit_unrecoverable`), or
  `never_released = true` (the version number exists but was never a
  release). The schema's current exactly-one rule between the recorded
  commit and the unrecoverable marker is relaxed to this three-state rule
  in the same edit that adds the new keys.
- **The recorded-commit vocabulary rename**: the user has banned the old
  jargon word for the recorded-commit concept fleet-wide. This campaign
  renames it throughout rlsbl: prose, error texts, identifiers, and docs
  say "the recorded commit" / "commit-recorded" / "commit-unrecoverable";
  the schema key renames as above; the retiring script's filename is
  quoted verbatim where it must be named. Scope note: the full-rename
  reading of the ban follows the same precedent as the release-record
  word's ban and rename; it is recorded in Decision origin as adopted from
  the ban order and freely reversible to a prose-only rename.
- **The shipped-as field**: a new optional archive field recording ANY
  historical tag spelling the version actually shipped under when it
  differs from the current scheme (a pre-rename spelling like
  `strictcli@v0.12.0`; a member-path spelling like `auth-gateway/v0.1.0`).
- **The transition record**: the committed append-style store of
  repository-level events, today named `lineage.jsonl` and renamed by this
  campaign to `transitions.jsonl` (command surface: `rlsbl transition
  record`). Its event kinds after this campaign: the pre-existing ones
  (history rewrites, conversions, boundary aliases, promotion commit maps)
  plus `release-history-closed` and `non-version-tag`. Two kinds are
  deliberately DISTINCT and never merged: a **releasable rename** is a
  tag-spelling fact (bookkeeping; reconcile's identity refusal does not
  match it), while an **identity change** (Go module path, registry name —
  what consumers resolve) keeps reconcile's refusal to recreate older
  refs, with zero exceptions.
- **The fate taxonomy (closed)**: every version number or tag is exactly
  one of: released; never-released; deliberate non-release tag;
  release-history-closed; non-version tag. Placement rule: a fact about ONE
  VERSION lives in that version's archive (a deliberate version-shaped tag
  is explained by its never-released archive, needing no separate record);
  an event about the REPOSITORY lives in the transition record. Anything
  unclassifiable forces a deliberate taxonomy extension.
- **The never-released declaration IS the archive**: to declare a version
  never-released, write (or amend) its archive with the marker BEFORE
  running the backfill; the backfill treats an existing never-released
  archive as settled and never records a commit for such a version from a
  bump commit. No flag and no input file exist for this on purpose (the
  existing-artifact rule in the user's rules file).
- **Adopt-as-released**: a version-shaped tag published on origin that no
  store records was a release; it gets an archive with its commit recorded
  from the tag, description recovered per the recovery chain, bump derived
  by version arithmetic, every reconstructed field marked with its source.
- **The identity ruling (minting)**: a renamed releasable's past versions
  get current-spelling tags minted at their recorded commits through the
  reconcile repair surface; the old-spelling tags remain in place,
  explained by their archives' shipped-as fields; once minted, a version's
  expected primary ref is the current spelling. The minting capability
  already exists in reconcile's materialize path (verified: it creates and
  pushes a locally-absent tag at a recorded commit); what this campaign
  builds is only the expected-spelling derivation.
- **The one spelling authority**: `expected_refs` on the target protocol
  remains the single authority for which spellings address a version. Its
  recorded-aliases reader is EXTENDED to consume shipped-as fields; if a
  recorded alias event and a shipped-as field both cover a version and
  disagree, that is a hard error, never a precedence guess.
- **The recovery chain** (for reconstructed archive descriptions), in
  order: GitHub Release body → the version's CHANGELOG.md section → commit
  subjects in the version's tag range → placeholder. A Release body with no
  substantive content counts as ABSENT and falls through (auto-generated
  compare-link boilerplate is not content; bullets, prose, and blockquote
  openings are). Every recovered value names its source in the archive.
- **Consequential** (per the user's rules file): the classification means
  ONLY A HUMAN GETS TO MAKE THIS CALL — an agent never self-approves. It
  has nothing to do with recoverability or the cost of the writes.
- **Managed-repo hygiene doctrine** (the user's ruling): rlsbl is maximally
  strict with the repos it manages. State the tooling cannot account for
  blocks the guarded operations until fixed: an unexplained tag blocks the
  backfill's apply; a git stash present in a managed repo is a hard error
  on guarded operations (release, backfill, reconcile) — drop it or
  nothing works. No overrides.
- **The pre-approved limits** for unattended outward writes, approved by
  the user with the marker [%%] (trust-adopted, freely reversible, never to
  be cited as deliberate intent):
  - Auto-apply is permitted only for: creating a GitHub Release on an
    archived version's existing tag; creating and pushing a tag at an
    archive's recorded commit under the version's expected spelling;
    pushing an archived tag origin lacks.
  - Only materialize / already-correct verdicts qualify. Deletions, moves
    of existing refs, force-pushes outside mirror flows, and any repair or
    refusal verdict never auto-apply.
  - Ceilings: 130 GitHub Release creations and 80 tag creations fleet-wide
    (approved at measurements of roughly 100 and 62; phase 0.6 re-derives
    the measurements, and a material excess pauses for reconfirmation).
  - Apply mechanic: before each apply, the written reconcile plan file is
    inspected item by item; every item must match an approved write kind
    and verdict or that repo's whole plan pauses (reconcile has no
    selective apply). The operator loop keeps the running tally against
    the ceilings in the campaign record file.
  - The user's standing authorization recorded here covers passing
    `--approve-consequential` on the fleet phases' consequential commands
    (reconcile, the backfill) — the flag is never passed on the agent's own
    judgment beyond this plan's scope.
  - Exactly two user-ordered exceptions, enumerated here and nowhere else:
    (1) a private project's milestone tags (the `phase-*-complete` /
    `audit-program-complete` family) are DELETED locally and on origin;
    (2) pgdesign's v1.0.1 retraction tag is PUSHED. Both are attended
    steps in their phases, via plain git, outside the auto-apply
    machinery, on the user's explicit standing order recorded here.
- **Consent pauses**, all of them: an outward-writing plan whose items fall
  outside the pre-approved limits; a disposition of history no record and
  no approved class ruling explains; a ceiling breach; a materially moved
  measurement underlying a recorded approval. Nothing else pauses.

## Standing conventions (binding on every phase)

- Implement+audit pairs; auditors are blind (spec plus tree, no git
  history, no implementation notes).
- Every Verify clause is implemented red-first, and every Verify clause
  asserting current-world behavior was probed for satisfiability when this
  plan was written (per the user's rules file); phases re-probe their own
  premises where the plan marks a measurement as planning-time.
- Every error message that names a remedy gets a test that performs the
  remedy verbatim and asserts the error clears.
- One safegit commit per work-item with its changelog entry; machine-
  generated files via `rlsbl commit`. Changelog entries carry `--type
  breaking` where the item removes or renames surface (the vocabulary
  renames, the remote-release deletion, the strictness refusals, the alias
  removal, the marker-key rename); those entries are what justify the
  minor bump.
- Relevant suites green at phase boundaries; test batches over the bare-run
  limit go through the sandboxed runner; anything touching GitHub Release
  bodies in tests is fully mocked (the sandbox denies sockets).
- Sweeps dry-run-first with expected-occurrence assertions and full diff
  review.
- Every phase — code and fleet alike — ends with a falsified-text sweep of
  the text its own changes made stale, in every repo it touched.
- Any archive or record field written by tooling after ship time carries an
  explicit reconstructed/derived note naming its source.
- Rulings that rest on measurements are recorded with them in the campaign
  record file; a materially moved measurement pauses for reconfirmation.
- New commands update the schema dump, regenerated docs, and the
  wiring/effects/consequential pinning tests; a new target-protocol
  attribute updates the axis inventory and regenerates the support matrix.
- One rlsbl release, at the end of the code phases (phase 6). Never push
  outside sanctioned flows; the two enumerated exceptions are the only
  hand-performed ref writes in the campaign.

## Decision origin

All rulings in this plan are the user's deliberate decisions, except these
trust-adopted elements (freely reversible, never to be cited as deliberate
intent): the pre-approved limits [%%]; the reconstructed-description format
(joined bullet text marked with its source); the shipped-as placement in
the archives; the migration-script port target (the command's engine
module); the member-key interim (constant plus binding test); the
full-rename scope reading of the recorded-commit vocabulary ban.

## Phase dependency notes

Phases run in numeric order. Within that: 0.5 (the renames) precedes the
code phases because it touches the same files phases 1-4 edit most; 1.1
precedes any archive carrying new keys (the validator rejects unknown
keys); phase 6 (the release plus install update) precedes phases 7-11;
phase 9's recordings precede phase 10's reconcile pass; phase 11's newly
migrated repos get their own reconcile within phase 11.

---

## Phase 0 — Foundations

Effort: medium.

### 0.1 Pin and toolchain

- Replace the machine-wide editable install with released rlsbl 0.119.0;
  commit the restore-command record (no foreign paths in it). Create the
  campaign record file named in the preamble.
- Install the strictspec CLI at EXACTLY the version the Python runtime and
  the generated validators carry (0.2.3 at planning; validator pairing is
  exact-match, so neither an older nor a NEWER CLI is acceptable — a newer
  one would restamp regenerated files and break every import, and the
  generated-floor preflight check would block the release). The
  upgrade-everything-to-latest step deliberately rides a later natural
  release, not this campaign.
- Verify: pinned rlsbl version reported; `strictspec` CLI version equals
  the runtime's exactly; restore record and campaign record committed.

### 0.2 Fleet alias pre-edit

- The legacy `dev_node = true` member key is in live use in these repos
  (planning-time measurement; re-grep before editing): strictcli, orxtra
  (two members), WWW, gamehome (two members), mobileinfra, incantino.
- Pre-edit EVERY use in strictcli, orxtra, WWW, and gamehome, replacing
  each with the two-key form (`dev_only = true` plus `releasable = false`
  where absent; a bare rename is not equivalent where the second key is
  missing). Note: of these four, only strictcli's workspace loads today —
  orxtra and gamehome are load-refused on their retired watch keys and WWW
  on its missing root member; the edits are plain file edits regardless.
- mobileinfra and incantino are deliberately excluded: they run the pinned
  pre-campaign rlsbl (which predates the `dev_only` spelling — verify that
  claim against the pinned version's source at execution), and their
  implicit-mode conversions (their own filed todos) own the transition.
  rlsbl-sandbox, the third implicit-mode repo, carries no `dev_node` key
  and needs nothing here; its conversion todo stands.
- Verify: strictcli's workspace loads under current rlsbl; for orxtra, WWW,
  and gamehome the loader's refusal output mentions only their known other
  causes and never the alias; grep confirms no `dev_node` remains in the
  four.

### 0.3 External todo filings

- selfdoc: (a) the nested-subgroup rendering gap — its group-page renderer
  iterates only a group's direct commands and never its nested groups, so
  a subgroup's commands appear on no page; (b) the inconsistent short-help
  measurement — for a choice flag it measures the member help on some
  flags and the member's VALUE help on others.
- strictspec: its runtime emits a migrate invocation its own CLI rejects
  (a flag spelling of a positional argument) and that cannot repair a
  document missing the format-version marker at all; the template is
  spec-pinned, so the fix is spec plus code there.
- strictcli: propose a framework-level supplied-but-empty flag stance, so
  per-consumer empty-string refusals eventually collapse into the
  framework.
- Verify: todos committed in all three repos.

### 0.4 Shared defect fixtures

- The cross-phase red set, each verified red in ITS OWN consuming phase
  (not all in one phase): a cross-filed changelog entry (commit in another
  releasable's territory) misreported as out-of-range (consumed by 4.2); a
  phantom version whose commit gets recorded from its version-bump commit
  (consumed by 2.1); a pre-marker archive missing required fields the
  current backfill leaves invalid (consumed by 2.1); unknown keys on each
  workspace configuration surface (consumed by 3.2); an explicitly-empty
  flag value silently dropped (consumed by 3.3).
- Verify: each fixture demonstrably red against current behavior.

### 0.5 The vocabulary renames (before the code phases)

- Three renames swept together through rlsbl prose, error texts,
  identifiers, and docs:
  - the old release-record word → "the release archives" / "the release
    record";
  - "lineage" → "transition record": the module, its identifiers, the
    store filename in code, the generated per-module doc pages the module
    rename orphans, and the selfdoc manifest module lists (no committed
    `lineage.jsonl` file exists anywhere in the fleet — verified — so no
    fleet data files need renaming);
  - the recorded-commit vocabulary: prose and identifiers move to
    "recorded commit" / "commit-recorded" / "commit-unrecoverable"; the
    schema-key half lives in 1.1; the retiring script filename
    `backfill_release_anchors.py` is quoted verbatim where it must be
    named until 2.1 retires it.
- Root files generated by selfdoc are edited via their templates under
  docs/, never the generated read-only outputs.
- Historical changelogs and archived release files stay untouched.
- Verify: grep for all three old words is clean outside immutable history
  and the not-yet-retired script; selfdoc baselines re-accepted after
  review.

### 0.6 Fleet measurement re-derivation

- Re-derive, per repo, into the campaign record file: the standalone
  rlsbl-managed repo set (planning-time measurement: 22); tags-without-
  archives (planning-time: roughly 100 across eight repos — safegit,
  claudewheel, howmuchleft, a private project, claudestream, predraw, saferm,
  wesktop); the inverse category, archives whose tags exist nowhere
  (planning-time instances: rlsbl, pgdesign); the projected outward writes.
- Confirm the approved ceilings (130/80) still bound the projection, or
  pause for reconfirmation.
- Verify: the per-repo table exists in the campaign record file; ceilings
  confirmed or escalated.

## Phase 1 — The version-fate model

Effort: large.

### 1.1 Schema and binding

- One edit to the release-file schema covering: the `never_released` key;
  the shipped-as field; the marker-key rename (`unanchorable` →
  `commit_unrecoverable`, no dual recognition — pre-stable, no compat);
  and the relaxation of the exactly-one rule to the three-state rule
  (commit-recorded, commit-unrecoverable, or never-released — exactly
  one). No format-version bump is obligated for the widening parts; the
  key rename is breaking and changelogged as such.
- Regenerate the validator with the exactly-matched toolchain; bind the
  fields in the archive reader; the editable release file refuses the new
  keys exactly as it refuses the recorded-commit fields (flow-owned).
- Migrate the committed archives carrying the old marker key: rlsbl's own
  one such archive now; strictcli's set needs no migration — phase 9
  rewrites those archives wholesale when their commits are recorded (they
  leave the marked state entirely).
- Verify: archives in each of the three states validate; the editable-file
  refusal fires for each new key; generated files carry the exact
  toolchain stamp; the old key is refused as unknown.

### 1.2 Read-path semantics for never-released

- Excluded from: the latest-release fact (the next real version wins); the
  unreleased-range computation; undo's latest-version pick (which today
  selects a phantom-topped repo's phantom and dies); ref-presence
  expectations (counted, never demanded); reconcile verdicts (skipped and
  counted; a still-existing tag of such a version is reported as
  explained, never deleted); the contains-latest refusal.
- Changelog generation renders a never-released version's section
  explicitly annotated as never released, from the archive's own
  description (a phantom can have real finalized changelog files; hiding
  the section would lose record).
- Status renders a distinct label; the JSON payload gains fields
  expressing all three archive states.
- Verify: per-consumer fixtures including generation's annotated section;
  undo on a phantom-topped fixture undoes the real latest.

### 1.3 Loud archive reads in changelog generation

- The raw-parse path that silently turns an unreadable archive into an
  empty description becomes a hard error through the real reader.
- Verify: a schema-invalid archive aborts generation loudly.

### 1.4 Transition-record kinds and the event line

- The transition-record schema gains `release-history-closed` and
  `non-version-tag`, with the same schema-edit-plus-validator-regeneration
  obligation as 1.1.
- The rename-vs-identity line is stated in the record's definitions:
  releasable renames are spelling facts; identity changes keep reconcile's
  refusal to recreate older refs, with zero exceptions. Verify at
  execution that no recorded event in the affected fleet repos is an
  identity change that the minting phases would collide with.
- Named consumers: the backfill (recorded non-version tags leave the
  unexplained listing), the residue check (5.5 exempts a member whose
  release history is recorded closed), reconcile (a recorded non-version
  tag is an explained ref).
- Verify: malformed records refused; each kind round-trips; regenerated
  validator stamped exactly.

## Phase 2 — The commands

Effort: extra large.

### 2.1 `rlsbl release backfill` (promoting the script)

- Why promotion: a shipped hard error (the empty-record-in-tagged-
  repository refusal) names the script invocation as its remedy, which an
  installed user cannot follow; and any adopter with pre-existing release
  history needs this operation at onboarding.
- Built on the preview/apply skeleton; mutating; CONSEQUENTIAL — under the
  user's definition this is a call only a human makes (it rewrites a
  repository's authoritative release record), regardless of the writes
  being local and recoverable. The pinned classification test's stated
  rationale (recoverability) is corrected to the human-authority
  criterion in the same change; see 4.4 for the re-derivation of the full
  classification set.
- Behavior — everything the current script does, plus:
  - Existing archives completed: ALL missing required fields recovered
    (description via the recovery chain, targets-derived include, default
    exclude), not only the format-version stamp.
  - The recovery chain as defined in Terminology, including the
    no-substantive-content rule; the materialized-archive header comment
    updated to enumerate the sources.
  - The never-released declaration honored as defined in Terminology (an
    existing never-released archive is settled; the bump-commit note tells
    the operator to declare before running).
  - Adopt-as-released for scheme-matching version tags no store records.
  - Shipped-as consultation so renamed and member-path history gets its
    commits recorded instead of counting as unexplained.
  - Recorded non-version tags excluded from the unexplained listing.
  - An operator-authored overrides input (reviewed descriptions applied
    before derivation), replacing the separate description-authoring
    script.
  - **Unexplained tags are a hard error**: preview lists the unexplained
    set first; apply refuses while any remain; the error names the cheap
    resolutions (adopt / record / delete per the user's standing class
    rulings). No partial completion. Per the managed-repo hygiene
    doctrine, a present git stash is likewise a hard error.
- Named reuse, not re-creation: the GitHub Release body reader is the one
  in the publication module — and the two remaining ad-hoc body readers
  (in deprecate and yank) are folded onto it in this same item;
  tree-at-commit resolution consolidates onto ONE shared helper (the
  copies in the release flow, the commit-remap module, extract, absorb,
  and the script collapse onto it); "is this tag explained?" is answered
  by ONE shared consultation function used by this command and reconcile
  (archives for version-shaped tags, the transition record for the rest).
- Both old scripts retire as removal stubs: `backfill_release_anchors.py`
  and the description-authoring script. The workspace migration script —
  which today runs the backfill script in-process — is PORTED to the
  command's engine module in this same item, keeping its own script form
  and exit contract.
- The shipped remedy is retargeted at every live mention site in this
  repository: the three in the archive-reading module, the one in undo,
  the release-workflow doc, the five test assertions naming the script,
  and both scripts' own test files (rewritten against the command); the
  hand-written multi-step recovery procedure in the missing-record error
  is replaced by naming the command.
- Verify: the 0.4 fixtures this phase consumes go green; idempotent second
  runs plan nothing; remedy-followability tests drive each printed remedy
  verbatim; grep confirms no live reference to either retired script; the
  migration script's tests pass against the ported engine; an
  unexplained-tag fixture and a stash fixture each refuse apply.

### 2.2 `rlsbl transition record`

- The typed door for operator-declared transition-record facts (the two
  new kinds), refusing unknown kinds; mutating; consequential status per
  the 4.4 re-derivation (the human-authority criterion decides it, and
  the user rules on the derived set); dry-run supported; `--auto-commit`
  as the standard opt-out boolean.
- Verify: each kind recordable; unknown kind refused; recorded facts
  change backfill, residue-check, and reconcile behavior as 1.4 names.

### 2.3 Expected-spelling derivation

- The minting capability already exists in reconcile's materialize path
  (verified at its call sites); this subphase builds only the
  expected-spelling derivation per the identity ruling, fed by shipped-as
  fields through the one spelling authority, with the
  disagreement-is-a-hard-error rule from Terminology.
- Verify: a renamed-releasable fixture in which reconcile's plan mints the
  current-spelling tag at the recorded commit while the old tag stands
  explained; a fixture where an alias event and a shipped-as field
  disagree hard-errors; the minted write matches the pre-approved write
  kinds.

## Phase 3 — Deletions and strictness

Effort: large.

### 3.1 Delete the remote-release feature

- The dispatch workflow template, both scaffold call sites and their
  helper, and the feature's test file; immutable changelog mentions stay;
  the manifest regenerates.
- Verify: no live reference; scaffold output carries no dispatch workflow.

### 3.2 Unknown-key refusal (the policed surfaces)

- The policed-surfaces list is written as a maintained section of
  docs/configuration.md, naming: workspace.toml (both table kinds and the
  top level) and the standalone releasable file — policed now — and
  .rlsbl/config.json as unpoliced pending its own later item (the
  config-schema completion and wiring, filed at close-out).
- The known key set for member tables gets ONE declared authority: a
  module-level constant with a test binding the loader's refusal and the
  wrapper's accessors to it (the member table has no dataclass to derive
  from; the constant is the stated interim the schema-wiring item
  replaces). Releasable tables keep their dataclass-derived set.
- Refusal at load on all policed surfaces; the `dev_node` alias deleted
  outright with a hard error naming the two-key remedy; the standalone
  releasable file aligned to explicit-or-absent for its tag format; the
  save path strips runtime-injected bookkeeping keys so they can never be
  persisted and then refused.
- The enumerated tolerant-behavior tests (the unknown-field and roundtrip
  tests in the workspace parsing/roundtrip suites, and the releasable
  losslessness tests) flip to refusal tests.
- Verify: refusal fixtures per surface naming the offending key; the four
  migrated fleet workspaces (strictcli, strictspec, stricttest, selfdoc)
  still load.

### 3.3 Empty-flag refusal

- Every explicitly-empty string flag value that is today silently treated
  as absent becomes a hard error naming the flag — implemented centrally
  through the framework's supplied-or-not predicate rather than per-site
  checks. The planning-time inventory spans the monorepo
  add/graph/impact/absorb/init commands, the batch-release-init filter
  (where an empty value silently selects every releasable), the dev
  include/exclude flags, and both target resolvers; re-grep at
  implementation.
- File-key empty-means-unset conventions are untouched.
- Verify: per-flag refusal tests through the central path.

### 3.4 Managed-repo hygiene refusals

- Per the user's doctrine: a git stash present in a managed repo is a hard
  error on the guarded operations (release run/resume, the backfill's
  apply, reconcile's apply), with the remedy naming the drop. Registered
  as a check as well as enforced at the operations.
- The dormant member-side reading paths are HARDENED (the user's ruling):
  a non-releasable member's own per-package changes directory is refused
  (not silently consumed) by the paths that would read it today — hash
  enumeration, the monorepo status fallback, prepush coverage — closing
  the class structurally, with 5.5's check as the detection layer.
- Verify: stash fixtures refuse each guarded operation; the check reds; a
  fixture giving a non-releasable member a changes directory is refused by
  each reading path.

## Phase 4 — Truthful surfaces

Effort: medium.

### 4.1 `changelog remove`

- A NEW command (not a mode of `changelog edit`, whose sparse-update
  construct exists to refuse property-less invocations): removes one entry
  selected by id or commits, atomic rewrite, released-file unlock flow.
  The two shipped error messages that today name a nonexistent removal
  flag are rewritten to name it.
- Verify: removal round-trips on unreleased and released fixtures; the
  printed remedy executes verbatim in a test.

### 4.2 Message corrections

- The range check distinguishes out-of-scope from out-of-range (the actual
  condition in the two blocked fleet repos, currently mislabeled).
- The orphans remedy names real invocations.
- The name-availability command's usage line names the command's real
  name.
- Status's release hint names the full release-run invocation instead of
  the bare command group.
- Status at a workspace root without a root manifest errors naming the
  monorepo status command.
- Verify: followability tests for each corrected message; the cross-filed
  fixture from 0.4 goes green here.

### 4.3 Help strings

- The four flag helps measured short at planning — `--pattern`, `--file`,
  `--from-commit` (scrub's two mode members and its range selector), and
  `--to-module` (the module rename target) — lengthened on BOTH the
  member and value sides, so the doc generator's inconsistent measurement
  passes either way.
- Verify: those warnings gone from the doc check.

### 4.4 Classification re-derivation

- With the pinned classification test's rationale corrected to the
  human-authority criterion (2.1), re-derive the consequential
  classification of every command under it and present any proposed flips
  to the user before phase 6 — the classification is by definition the
  human's call.
- Verify: the pinned test's rationale text states the human-authority
  criterion; the derived set is recorded in the campaign record file with
  the user's rulings on any flips.

## Phase 5 — Hygiene

Effort: medium.

### 5.1 Dead and unused

- The zero-production-caller ownership helpers deleted (the ones found at
  planning: `owners_of_files`, `unowned_paths`, `tool_owned_rules`, and
  the scope constructor with no callers — re-verify callers at
  execution); unused parameters dropped; the stale "future Flutter
  target" comment corrected.
- Verify: grep shows no callers existed; suite green.

### 5.2 Resolution memoized

- The changelog-context resolution cached per check-run context (measured:
  nine-plus full re-resolutions per check invocation today).
- Verify: a counter test pins one resolution per context.

### 5.3 Ruff over tests

- Lint scope extended to the test tree; auto-fixes applied; the
  assigned-unused findings reviewed semantically.
- Verify: repo-wide lint clean at the configured rules.

### 5.4 Flutter entry points

- The Flutter target overrides dead-module entry-point derivation for its
  main-file convention (today a Flutter app with no derivable entry
  silently analyzes nothing and reports clean).
- Verify: a Flutter-app fixture analyzes from its real entry point; the
  axis inventory and support matrix regenerated if any answer moves.

### 5.5 Residue-check extension

- Release state (archives, changes, scheme-matching tags) on a dev node or
  non-releasable member becomes a finding — unless the transition record
  marks that member's release history as closed.
- Verify: red fixture; a closed-history record silences it.

### 5.6 Reconcile plan files become ignored run-state

- Per the user's clarification of the committed-generated-files rule
  (durable artifacts are committed; per-run state consumed by its own flow
  is exempt): the reconcile plan file joins the scaffold's gitignore
  template, so the fleet pass leaves no dirty trees.
- Verify: a scaffolded repo ignores it; the falsified-text sweep covers
  any doc stating it must be committed.

## Phase 6 — The release

Effort: medium.

- Blind audits covering phases 0.2 through 5 (the fleet pre-edit and the
  fixtures included), fix rounds as needed, then the single minor release;
  the pinned install updates to the new release.
- Verify: registries serving the new version; suite and checks green; the
  new version's archive carries its recorded commit.

## Phase 7 — Standalone-fleet sweep

Effort: large. Runs on the released rlsbl via the new backfill command,
with `--approve-consequential` under the standing authorization recorded
in the limits section.

- Per repo across the measured sweep set from 0.6: dry-run reviewed, then
  apply and commit. Expected per the measurements: materializations with
  recovered descriptions; early-tag adoptions (safegit and claudewheel
  recover real content from their own GitHub Release bodies; howmuchleft's
  bodies are boilerplate, so its recovery falls through to commit
  subjects); tinymoon's tagless version gets its commit recorded from its
  bump commit.
- a private project: FIRST the user-ordered exception — its milestone-tag
  family deleted locally and on origin (attended, plain git, per the
  standing order in the limits section) — then its backfill.
- Verify: every guarded read (status, unreleased) works across the whole
  sweep set; second runs plan nothing; the a private project tags verified
  absent both locally and on origin; per-repo falsified-text sweep.

## Phase 8 — pgdesign (attended, single merged visit)

Effort: medium. Re-probe the repo's state first (its dirty tree and
uncovered commit found at planning were both resolved within a day —
fleet state moves).

In this order:

1. The v1.0.0 archive rewritten from the commit-unrecoverable wedge to
   never-released.
2. The self-inclusive retract directive added to go.mod
   (`retract [v1.0.0, v1.0.1]` with a comment stating v1.0.0 was an
   accidental tag, never a release), committed with its changelog entry.
3. The v1.0.1 archive written never-released with the retraction note —
   BEFORE any push, so the record precedes the ref.
4. The second user-ordered exception: the v1.0.1 tag created at that
   commit and pushed via plain git, attended.
5. The proxy poked to fetch v1.0.1; resolution verified flipped: the go
   tool's latest query answers the real highest 0.x, and both 1.x
   versions leave default version listings.
6. Text made true in the same phase: pgdesign's own rationale paragraph
   about the phantom, rewritten to the post-retraction reality.

- Verify: the go tool resolves latest to 0.27.x; both 1.x versions absent
  from default listings; all pgdesign checks green, including generation's
  annotated never-released sections.

## Phase 9 — Workspace-fleet repairs

Effort: large.

- stricttest and selfdoc: the cross-filed changelog entries removed via
  `changelog remove` (both are unreleased-file edits — no released-file
  unlock, no GitHub write); selfdoc's uncovered commit covered.
- strictcli: the backfill records the wrongly-unrecoverable archives'
  commits from their real old-spelling tags, writing each version's
  shipped-as field in the same unlock pass; ONE transition-record entry
  records the rename event itself as a spelling fact (reason and scope
  stated once); the unmatched early tags adopted as released; the
  conformance member's release history recorded closed; the local-only
  tag left for phase 10.
- strictspec and stricttest: their old-spelling tag sets handled the same
  way (shipped-as fields plus one rename spelling-fact each).
- Verify: changelog and workspace check tags green in all four; no archive
  remains marked commit-unrecoverable whose tag exists anywhere.

## Phase 10 — The reconcile pass

Effort: medium.

- Reconcile plans across every eligible repo AT THIS POINT (the phase 11
  repos are excluded here and covered in phase 11), auto-applied under the
  pre-approved limits with the stated apply mechanic: the minted
  current-spelling tags at recorded commits, the missing GitHub Releases,
  the archived-but-unpushed tags. Pauses only per the consent-pauses list.
- Verify: ref-presence checks green across the repos covered so far; the
  running tally within ceilings in the campaign record file; a written
  summary of every ref created.

## Phase 11 — Root-kind rollout and close

Effort: large.

- orxtra via the ported migration script (tag format written on its
  releasable, watch keys deleted; its alias keys were fixed in 0.2), whose
  backfill half now runs the promoted engine — no separate backfill step.
- a private project: its untracked files inspected and committed first, then the
  dev-node root edit via the script.
- WWW: the dev-node root edit; its two member-path tags get shipped-as
  fields on the versions they shipped (the widened definition covers
  member-path spellings).
- gamehome: deferred to its release hold. Its todos carry the decision and
  the two hand-steps the script does not perform (the dangling depends-on
  rewrite after the root member rename; the per-family tag reckoning).
- Then, in order: reconcile runs for the repos newly migrated in this
  phase (same limits, same tally); the editable install is restored; the
  closing fleet pass runs (status plus cheap checks in every repo — by
  hand this once; the fleet health runner is another project's filed
  todo); the deferred items are filed as todos in this repo's backlog
  (the batch release-file reader's unification onto the schema authority;
  the config-schema completion and wiring named by the policed-surfaces
  list); completed todos move to done with the campaign record file; and
  the final report reconciles every measured outcome against its recorded
  approval, including the final write tally against the ceilings.
- Verify: the three migrated workspaces load and pass their workspace and
  changelog check tags; their reconcile plans applied within limits; the
  editable install restored last; the filings and moves committed.

## Deliberately not in this campaign

- The config-file unknown-key refusal (the config-schema completion and
  wiring): named by the policed-surfaces list in docs/configuration.md;
  filed at close-out.
- The batch release-file reader's unification onto the schema authority:
  filed at close-out.
- The strictspec toolchain upgrade-to-latest in lockstep: rides a later
  natural release.
- The three implicit-mode workspace conversions (their own filed todos, on
  the pinned pre-campaign release, on their own schedule): rlsbl-sandbox,
  incantino, mobileinfra.
- gamehome's migration (blocked on its release hold; its todos carry the
  decision and the hand-steps).
- The fleet health runner (another project's filed todo).
- The framework-level empty-flag stance (strictcli's todo, filed in 0.3);
  rlsbl's central predicate use in 3.3 is the consumer-side interim.
