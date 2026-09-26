# Lifecycle status per releasable, recorded in the transition record

## Context

Nothing in rlsbl records where a releasable is in its life. Concrete effects:

- A shelved project can keep live npm and PyPI packages with no notice, and
  nothing in its repository, its registries, or any generated documentation
  says it is shelved.
- Documentation rosters and a landing site's tool data copy names and
  versions by hand and drift from rlsbl's own release records.
- rlsbl was created by renaming an earlier project, share-it-on. On npm,
  share-it-on was published up to 0.4.6 and then unpublished entirely on
  2026-04-30, shortly after rlsbl's first commit; it never existed on PyPI.
  rlsbl's version count restarted at 0.1.0, and the only trace of the
  connection is prose in the first release archive and in `CHANGELOG.md`.

Every decision below was made by the owner after a review of an earlier
draft, which found that its status commit could never reach the remote, that
Go deprecation cannot follow a final release, and that it stored one fact in
two places.

## Decided design

### States and where they are recorded

- The status is declared **per releasable**. A standalone project has one
  releasable; a monorepo has one status per releasable. Repository-level
  effects are derived from the releasables, never declared separately.
- The states are `active`, `on-hold` (with a reason), and `retired` (with a
  reason and a successor, described below).
- **The transition record is the single authority.** Each status change is an
  event appended to the releasable's transition record. There is no status
  key in any configuration file. A releasable with no lifecycle event is a
  hard error; there is no default.
- The record's documented promise changes. It currently says it records
  history and never drives behavior (`rlsbl/transition_record.py` and its
  schema under `.strictspec/`). It is to say instead that every line is a
  fact written by the operation or declaration it records, and that its
  lifecycle events decide whether releases may run.

### How the record is protected

No check inspects who wrote a line. Two structural properties replace that:

1. **Append-only:** the record as it stood at the last release must be an
   unchanged prefix of the current record. Any edit or removal of an
   existing line fails this comparison.
2. **Verified consequences:** each state's external effects are checked
   against the registries and GitHub, never trusted. A hand-typed line
   therefore cannot produce a false state: it either breaks the prefix or
   declares something the registries or GitHub contradict.

With both in place, rlsbl's existing instruction to operators to edit record
lines by hand can only express something true.

### Reading the status

- A read-only subcommand of a new lifecycle command group reports, as JSON
  with a declared payload schema, each releasable's status, reason, and
  successor, plus two derived facts: a committed rename not yet released
  (read from identity transitions), and "not yet released" for a releasable
  with no release archive under `.rlsbl/releases/`. "Not yet released" is
  never declared.
- The command that appends a lifecycle event also regenerates a committed
  status file listing the current status per releasable, with a freshness
  check, the same way `CHANGELOG.md` is generated from `.rlsbl/changes/`.
  Tools that do not run rlsbl read that file.

### How a status change reaches the remote

- Releases are the only push. `on-hold`, and a return to `active`, are
  allowed only at a release boundary: the lifecycle command refuses unless
  main has no commits since the last release, so pushing main publishes
  exactly the one lifecycle commit. It does not build commits with git
  plumbing: doing so would make local and remote main diverge, forcing
  either replayed commits with new hashes (rlsbl's changelog entries
  reference commit hashes) or merge commits at the next release.
- The retirement event is part of the final release's own commit.

### What each state refuses

`on-hold` and `retired` both refuse every command that publishes new state:
`rlsbl release run`, batch release planning in `rlsbl monorepo release run`
(the refusal belongs where the batch plan is resolved, before any member
commits), `rlsbl release resume`, and `rlsbl release retry`. Commands that
repair or withdraw what already shipped stay allowed: `rlsbl release
reconcile`, `rlsbl release yank`, and deploying an existing version.

These refusals belong in release validation, before any mutation, not in
preflight checks: when a pre-release hook is customized, the built-in
preflight checks are skipped (`rlsbl/commands/release/__init__.py`).

`on-hold` changes nothing on any registry.

### Retirement

Retiring a releasable that has never released is refused. Otherwise
retirement proceeds in this order, and the command is re-runnable: each step
checks the registry or GitHub before acting, so a failure midway is finished
by running it again. It refuses while a release of that releasable is in
progress.

1. **A final release.** Its commit contains the retirement event, and the
   release's `CHANGELOG.md` section and GitHub Release notes gain a
   retirement section generated from the event's reason and successor. For
   a Go module, the `// Deprecated:` comment in `go.mod` must be part of this
   release, because Go reads a module's deprecation from the `go.mod` of its
   latest version.
2. **Registry deprecation, each verified by reading the registry:**
   - npm: the whole package deprecated, naming the successor. rlsbl has no
     whole-package deprecation today; the target protocol has only
     per-version `yank` (`rlsbl/targets/protocol.py`).
   - Go: the module proxy reporting the deprecation shipped in step 1. The
     existing `go-deprecation-published` check covers only old module paths
     after a rename, so a retired module needs its own check, reusing the
     probe.
   - PyPI: the project reporting `archived`. PyPI has no deprecation
     command. Its index API reports a project status when requested with
     `Accept: application/vnd.pypi.simple.v1+json` (for example
     `"project-status": {"status": "active"}`); owners set `archived` in the
     web interface and can unarchive at any time; archived projects stay
     installable and refuse uploads. The command prints the manual step and
     verifies the result. PyPI's status carries no reason, so a successor
     cannot be named there.
   - Every other target (for example hex, maven, dart, flutter, deno,
     docker, swift, zig, and pgdesign): retirement is refused for a
     releasable publishing to any target that has no defined, verified
     retirement step, naming that target, until the target gains one.
3. **Repository archiving,** through `PATCH /repos/{owner}/{repo}` with
   `{"archived": true}`: the repository is archived once every releasable in
   it is retired, and a mirrored releasable's own mirror repository is
   archived when that releasable retires. The existing `old-repo-archived`
   check and its probe in `rlsbl/transition_record_followup.py` should be
   reused for verification.

**The successor** is an explicit name per registry the retiring releasable
publishes to, each either a name or an explicit none, plus an optional site
address for redirects.

**Revival** reverses the order: unarchive the repositories first (an archived
repository is read-only, including tags and releases), then withdraw the
notices, and verify PyPI reports the project no longer archived before any
release, since an archived PyPI project refuses uploads. Reviving a Go
module's deprecation takes a release without the `// Deprecated:` comment.

### Renames

- A planned rename is not a state. A committed rename is read from the
  record's identity transitions. `rlsbl rewrite project-name` writes those
  only for standalone projects, and refuses inside a workspace; a monorepo
  package-name rename has no rlsbl writer yet, which this work needs to
  address.
- Releases are allowed while a committed rename is not yet released: that
  release is what ships the new identity.
- After a rename ships, a check requires each old package name to be
  deprecated naming the new one, or to have nothing left to deprecate (a name
  unpublished or never published). `rlsbl rewrite project-name`'s printed
  remaining steps should include this.
- A new event kind, **renamed-from**, records a rename after which the version
  count restarted: the old name, its last version, the new name's first
  version, and a per-registry outcome for the old name (deprecated,
  unpublished, or never published). The existing identity-transition event
  cannot express this, because it assumes one continuous version line. It is
  declared through `rlsbl transition record`, and it must state which side of
  the record's rename-versus-identity distinction it is on, since reconcile's
  identity refusal matches on that. rlsbl's own first event: share-it-on
  0.4.6 followed by rlsbl 0.1.0, npm unpublished, PyPI never published.

### Release history closed

The existing release-history-closed declaration stays independent. It says a
version line ended at this location (for example after an extraction or a
rename, where the package continues elsewhere) and triggers no retirement or
registry step.

### Adoption

Once a lifecycle event is required, every existing releasable fails until it
has one, and nearly every rlsbl command loads releasables, so the migration
must run while events are still absent. The owner reviews a proposed status
for each releasable, derived from its recent activity, and confirms each one
that is not plainly active; a one-time migration command then appends the
initial events. Every writer that creates a releasable (scaffold, extract,
absorb) must append its initial event, and scaffold must require the status
explicitly rather than defaulting it.

### Downstream behavior

Retired releasables are removed from the documentation site and the landing
site, with redirects to the successor's site address where one exists.

## Folded-in fix

The docstring of `yank` in `rlsbl/targets/pypi.py` says nothing can verify a
PyPI yank and relies on operator confirmation. PyPI's JSON API
(`https://pypi.org/pypi/<name>/json`) reports `yanked` and `yanked_reason` per
release file, so the yank can be verified. Fix it with the same registry
reading the retirement checks need, with a red-green test.

## Constraints

- No implicit defaults, refuse over guess, hard errors rather than warnings,
  and no escape hatch that disables a refusal.
- Every error message that names a fix gets a red-green test that performs
  the fix and asserts the error clears.
- Checks that can block a release read only inputs the repository owns; the
  registry and GitHub verifications are networked follow-up checks declared
  as such.
- Plain descriptive names for every new command, event kind, and field.
- The lifecycle command group and the whole-package deprecation need names
  distinct from the existing `rlsbl release deprecate`, which marks GitHub
  Releases, and `rlsbl status`.

## Affected areas

- `rlsbl/transition_record.py`, its schema under `.strictspec/`, and
  `rlsbl/commands/transition_record_cmd.py`: lifecycle and renamed-from
  events, the append-only prefix check, and the rewritten promise.
- Release validation in `rlsbl/commands/release/` and batch planning in
  `rlsbl/commands/monorepo/`: the refusals.
- A new lifecycle command group registered in `rlsbl/__init__.py`, with its
  status file generator.
- `rlsbl/targets/`: whole-package npm deprecation, PyPI status and yank
  readers, and repository archiving.
- `rlsbl/data/checks.toml` and `rlsbl/checks/`: the verification checks.
- `rlsbl/commands/rewrite/project_name.py`, `rlsbl/commands/init_cmd.py`, and
  the monorepo extract and absorb commands: initial events and remaining
  steps.
- Documentation under `.stricttools/docs/`.

## Effort

Large: a new command group and event kinds, registry readers across several
registries and GitHub, new checks, changes to scaffold, extract, and absorb,
and an adoption pass across every managed project.
