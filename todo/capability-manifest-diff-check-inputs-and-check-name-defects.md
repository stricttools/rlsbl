# A capability-manifest diff on release, declared check inputs with a repo-only preflight, and six check-name defects

## Context

Between 2026-09-13 and 2026-09-16 a design round over the fleet produced
three items that are rlsbl's: two new preflight-class behaviours the owner
ruled on, and a set of defects in `check-name` observed in a real naming
session. Line references and behaviours are as of 2026-09-15; verify before
acting.

## 1. A widened capability manifest requires a changelog entry

**Problem.** Programs built on a runtime over strictcli publish, in their
schema dump, a checker-proven manifest of everything the program can touch:
per command, the path prefixes it may read and write, the argv prefixes it may
run, the URL prefixes it may reach, its purity level, and the child-process
vocabularies it reads through. Nothing compares that manifest across
releases. A release that adds `git push` to a command that previously only
read ships with no more notice than a typo fix.

**Decision (owner, 2026-09-15, on recommendation).** A check, tagged
`preflight`, that reads the manifest object from the project's schema dump
(the namespaced runtime object strictcli is to reserve for it), diffs it
against the manifest recorded for the previous release, and treats any
widening — a new prefix, a raised purity level, an added vocabulary — as
requiring coverage by a user-facing changelog entry, the same way a commit
needs coverage today. Preflight blocks otherwise. A narrowing needs nothing.
The manifest is recorded in the release record so the next release has
something to diff against; the first release with a manifest records it and
passes.

**Affected.** A new check under `rlsbl/checks/`, registered in
`rlsbl/data/checks.toml` with `project`-style targeting on projects whose
schema dump carries the object; the release record's format (one new field);
`docs/checks.md`. The check depends on strictcli reserving the object (filed
there). Red-green: a fixture whose manifest widens between two recorded
releases, refused; the remedy — a user-facing entry naming the widening —
executed in the test and asserted to clear it.

**Effort.** Medium.

## 2. Every check declares its inputs; preflight refuses machine inputs; a fixture run proves it

**Problem.** A release-blocking check today may depend on inputs no
repository owns. The concrete instance: the docs check that preflight runs
reads its accepted-word list from a single machine-local, unversioned file
outside every repository, so releasing rlsbl 0.121.1 required editing that
file, and the identical committed docs fail the identical preflight on any
other machine. The same shape — two things that must agree with nothing
owning the agreement — was found four times in one session.

**Decision (owner, 2026-09-15, on recommendation).** Three parts, all
required:

- **Declared inputs.** Every check declares what it reads, as a registry row
  beside severity and tags: `inputs = ["repo"]`, `["repo", "machine"]`,
  `["repo", "network"]`. This is the same shape `needs_network` already has;
  `needs_network` may become `network` in the `inputs` list, or stay beside it
  — one authority, not two.
- **Preflight refuses machine inputs.** `rlsbl check --tag preflight` refuses
  to run a check declaring `machine`, reporting the *check* red with the
  input named and the remedy: make the check repo-only, or take it out of
  preflight. A check that wants to be in preflight has to be repo-only, and
  the registry proves it.
- **The declaration is verified, not trusted.** rlsbl's own suite runs every
  preflight-tagged check against its fixture with the home directory pointed
  at an empty one. A check that secretly reads a machine file sees nothing
  there and fails its fixture in rlsbl's CI, so the `repo` row cannot go
  false silently after someone adds a hidden read.

The docs check in question is to gain a repo-only mode that consults only a
committed per-project accepted-word list (that list is filed with the docs
tool), and preflight invokes that mode.

**Affected.** `rlsbl/data/checks.toml` (the field and every row), the check
runner's tag filter, `docs/checks.md`, the test harness (the empty-home
run), and the five-place registration roster test. Red-green: a fixture
check declaring `machine` refused from preflight; a fixture check declaring
`repo` that reads `~/x` failing the empty-home run.

**Effort.** Medium.

## 3. Six `check-name` defects, from one naming session

Observed on 2026-09-14 while checking candidate names on npm and PyPI.

1. **It emits a name its own CLI refuses as input.** After reporting
   `moniker conflict with '-gexpress'`, both `check-name --target=npm
   -gexpress` and the quoted form were refused as an unexpected argument. The
   terminator that would fix it is strictcli's (filed there); once it exists,
   the moniker-conflict message should show the working spelling.
2. **Ctrl-C produces a raw traceback** from `time.sleep` inside the variant
   checker (`rlsbl/commands/check.py:332`). The interrupt handling is
   strictcli's (filed there); this note records that `check-name` is the
   command most likely to be interrupted.
3. **Multi-name, multi-target output is unlabeled and looks duplicated.**
   `check-name --target=npm --target=pypi one two` prints the same table
   twice with identical headers and no target name on either. Label each
   table with its target, or emit one table with a column per target.
4. **Batch mode silently loses depth.** Single-name mode reports moniker
   conflicts, insertion variants, the stdlib collision check,
   ultranormalisation and the PyPI prohibited-list caveat; batch mode reports
   `taken`/`available` and nothing else, and does not say so. Either report
   the same depth or state in the footer that batch mode is a triage pass.
5. **A cap notice prints before the check it belongs to.** `PyPI insertion
   variants capped at 30 for 'goexpression'` appeared above `Checking PyPI
   for "goexpression"...`, so it reads as belonging to the npm pass that had
   just finished.
6. **"taken" conflates *exists* with *blocked*.** `gexpress` was reported
   "taken on npm" because a package literally named `-gexpress` (two versions
   in two days, keyword `hack`, no repository) shares its moniker under
   npm's dash-dot-underscore rule; `gexpress` itself does not exist on npm.
   The verdict is right and the wording is wrong: report `blocked by moniker
   collision with '-gexpress'` when the exact name is absent and a moniker
   match exists.

Also: the `Increase --delay if rate limited` advisory prints on every batch
run whether or not anything was rate-limited; print it only after a
rate-limit response.

**Affected.** `rlsbl/commands/check.py` (`run_cmd`, `_check_single_name`,
`_check_variants`, the batch renderer). Effort: small each; item 6 needs a
fixture with a moniker-only collision.
