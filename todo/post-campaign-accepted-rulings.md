# Accepted post-campaign rulings: the next natural release's work batch

## Context

The conversion/ownership/ledger campaign shipped as 0.118.0 (+0.118.1).
During its closing review, the remaining open design questions were put to
the user one by one; the items below are the accepted rulings that require
rlsbl code changes. They are deliberate decisions, not proposals — each
needs implementation with the usual red-green discipline and rides the
next natural release together (release once, at the end of the batch).

## The items

### 1. A releasable owns its own state directory

The ownership model assigns `.rlsbl-monorepo/releasables/<name>/**` to the
root member, because no member's path claims it. Consequence: commits to a
releasable's own release machinery (archiving its release file, finalizing
its changelog) fall outside that releasable's changelog scope. Observed in
freshly migrated workspaces as orphaned changelog hashes whose commits
touch only the releasable's own state directory.

Ruling: the releasable itself owns its state directory. Extend the
ownership resolution (`rlsbl/ownership.py` and whatever threads member
lists into it) so paths under a releasable's state directory attribute to
that releasable's scope for changelog purposes. Mind the design seam:
ownership is member-based and a releasable is not a member — the coherent
shape is scope-level attribution (the releasable's changelog scope claims
the directory), not inventing a phantom member. The migrated workspaces'
orphaned hashes should resolve without per-repo edits once this ships.

Affected: `rlsbl/ownership.py`, `rlsbl/git_util.py` scope filtering,
changelog validation scope, the property tests in `tests/test_ownership.py`.
Effort: medium — the rule is small, the consumers and tests are the work.

### 2. Flutter joins the import-analysis check scope

During the target-protocol migration, Flutter (which extends the Dart
target) was deliberately pinned OUT of `supports_import_analysis` /
`supports_circular_dep_analysis` to keep behavior identical (the pin is in
`rlsbl/targets/flutter.py`, rebinding the detector methods back to
`BaseTarget`). Ruling: the pin was migration caution, not design — remove
it so Flutter answers like Dart. Update the pinned scope test
(`tests/test_target_axis_check_scopes.py`), regenerate the support matrix.
Effort: small.

### 3. `monorepo add` creates releasables like absorb does

`monorepo absorb` auto-creates a singleton releasable for an arriving
package; `monorepo add` can only join an existing one or opt out, leaving
"declare a new releasable" as a hand edit of `workspace.toml`. Ruling:
`monorepo add` gains the same auto-singleton creation — an add naming a
new releasable creates the `[[releasables]]` entry with an explicitly
written `tag_format` derived from the member's primary target scheme (the
same derivation absorb uses, including the mixed-scheme hard error naming
an operator-declared format as the remedy), and `monorepo sync` scaffolds
its state. Existing-releasable joins and `releasable = false` unchanged.

Affected: `rlsbl/commands/monorepo/commands.py`, the absorb tag-format
derivation (share it, do not copy), docs. Effort: small-medium.

### 4. `unpublished-refs` gains GitHub Release presence

The retired `github-release` check's standing coverage was never replaced:
Release presence is only visible on the repair path (`release reconcile
--plan` emits a materialize verdict for an anchored, tagged version with
no Release). Ruling: extend the `unpublished-refs` check with a
missing-Release finding — the check already holds each version's expected
set and performs one listing; an anchored version whose tag exists but
whose Release is absent is a distinct error naming `rlsbl release
reconcile` as the remedy. Unanchorable versions keep the counted-not-
errored treatment. Registration surfaces update in place (no new check).

Affected: `rlsbl/checks/release.py`, its tests, docs/checks.md row text.
Effort: small.

### 5. Unrecognized Gradle dependency patterns fail closed in derivation

Router filter derivation hard-errors on an unreadable manifest, but a
parseable Gradle file containing a dependency line the scanner does not
recognize still warns-and-continues — a distinct class that can narrow a
member's CI filter just as silently. Ruling: during ROUTER DERIVATION
(sync, the freshness check, the release-time window simulation) an
unrecognized Gradle dependency pattern is a hard error naming the file and
line, with declaring the edge in `depends_on` as the remedy; tolerant
consumers (graph and impact rendering) keep the warning, mirroring the
unreadable-manifest split exactly.

Affected: `rlsbl/workspace_graph.py` (the Gradle scanner's warn sites gain
the same collected-error exposure the read failures got),
`rlsbl/router_filters.py` (already refuses on collected scan errors —
verify the new class flows through), tests. Effort: small-medium.

### 6. Rename `monorepo release init --packages` to `--releasables`

The flag now validates its values as releasable names (implicit mode and
its packages sections are gone) but kept the old name; the help text
already says it takes releasable names. Ruling: rename to `--releasables`,
old spelling deleted outright (no alias, no compat), changelogged
breaking. Update the schema dump, docs, and wiring tests.

Affected: `rlsbl/__init__.py` registration,
`rlsbl/commands/monorepo/batch_release_init.py`, pinning tests. Effort:
small.

### 7. Delete the `release run` direct flags `--bump`, `--description`, `--preid`

These skip the release file entirely, which contradicts the file-driven-
over-flag-driven design rule the rest of the release flow enforces (the
release file forces stating intent before executing). Ruling: delete all
three; `.rlsbl/releases/unreleased.toml` is the one way to state a
release's intent. Changelogged breaking. Also delete the matching
"these flags exist but do not reach for them" paragraph from the rlsbl
reference section of the user-level instruction file at
`~/Projects/CLAUDE.md` (rlsbl is that file's subject there, and the
paragraph exists only because the flags do).

Affected: `rlsbl/__init__.py` registration and the release entry path,
schema dump, docs, wiring/consequential tests, the instruction-file
paragraph. Effort: small.

## Discipline

Red-green per item, one commit per item via safegit, changelog entries per
the usual classification (items 2-5 fixes/features as they read; 6 and 7
breaking; 1 is a fix from the consumer's view — orphaned coverage). One
release at the end of the batch, not per item.
