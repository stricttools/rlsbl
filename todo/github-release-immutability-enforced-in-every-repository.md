# Enforce GitHub release immutability in every repository, after reworking the commands it would break

## Context

GitHub repositories have a setting, "Enable release immutability" (generally
available since October 2025). With it on, every release published afterwards
is locked:

- its assets can no longer be added, replaced, or deleted;
- its tag is locked to its commit and cannot be moved or deleted while the
  release exists;
- if the whole release is deleted, its tag name can never be used again in
  that repository, nor in a new repository created under the same name;
- the release gets a signed attestation, checkable with `gh release verify`.

The title, the notes, and the pre-release and latest flags stay editable.
Releases published before the setting was turned on stay mutable, and drafts
are never locked.

The setting is readable and writable through the REST API, both needing
administration permission on the repository:

- `GET /repos/{owner}/{repo}/immutable-releases` reports the setting;
- `PUT /repos/{owner}/{repo}/immutable-releases` turns it on;
- each release object in the Releases API carries an `immutable` field.

Sources: https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases
and https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/

## Problem

The user wants the setting on in every repository rlsbl manages, enforced by
rlsbl, because it stops a stolen token from moving a tag or swapping an asset
under a version that already shipped. Turning it on today breaks four parts
of rlsbl:

1. **Asset uploads.** `create_args` in `rlsbl/release_publication.py` creates
   the release published, not as a draft, and
   `rlsbl/commands/release/publish.py` uploads artifacts afterwards with
   `gh release upload <tag> ... --clobber`. On an immutable release that
   upload is refused.
2. **`rlsbl release undo`** deletes the release and its tag so the version can
   be released again. The deletion still works, but GitHub refuses the tag
   name forever afterwards, so the freed version can never ship.
3. **`rlsbl release reconcile`** has a `re-point-with-lease` verdict that
   force-moves a tag to the commit the records name. A locked tag cannot move.
4. **`rlsbl release scrub`** rewrites history and re-points tags onto the
   rewritten commits. Locked tags stay on the old commits.

Unaffected: `rlsbl release deprecate`, `rlsbl release yank`, and notes
re-syncs through `rlsbl release edit` and the changelog commands, since they
change only notes and flags.

**A defect independent of immutability.** In `publish.py`, a failed asset
upload is caught and printed as `Warning: asset upload failed for pipeline
...`, and the release continues. A release can therefore ship with missing
assets while reporting success, which contradicts the family's rule that a
wrong state is a hard error, never a warning. With immutability on, every
upload would fail this way, silently.

## Required work, in dependency order

1. **Draft, then publish.** Create every GitHub Release as a draft, attach
   every declared asset, verify the draft carries all of them, then publish.
   This is the flow GitHub itself recommends for immutable releases. A failed
   upload is a hard error that leaves the draft unpublished and names the
   asset and the pipeline. `--clobber` goes away.
2. **`undo` under burned version numbers.** Once a version's immutable release
   exists, its number can never be reused. `undo` on such a version must
   either refuse, or delete the release and record the version as
   `never_released` in its archive, so the next release takes the next number.
   The choice is a decision for the user; the record model already has the
   `never_released` fate for burned numbers.
3. **`reconcile` without re-pointing.** For an immutable release,
   `re-point-with-lease` is impossible. The verdict must become a refusal that
   names the tag, the commit it is locked to, and the commit the records name.
4. **`scrub` refuses history under immutable tags.** A rewrite that would move
   a commit an immutable release's tag points at is refused before anything is
   rewritten, naming the releases in the way.
5. **Enforcement.** After 1 to 4 ship:
   - `rlsbl scaffold` turns the setting on for the repository it creates;
   - a release refuses to start when the setting is off, naming the setting
     and the command or page that turns it on;
   - an `rlsbl check` rule reports the setting per repository.

   Reading the setting needs administration read permission; a token without
   it is a hard error, never a skipped check.
6. **Rollout.** Turn the setting on across every repository rlsbl manages,
   once every repository is on an rlsbl version that contains 1 to 4.

## Open decisions for the user

- What `undo` does to an immutable release: refuse, or delete and burn the
  number as `never_released`.
- Whether the release-time refusal applies to repositories whose releases
  carry no GitHub assets at all (tag locking alone still protects Go
  consumers who bypass the module proxy).
- Whether rlsbl turns the setting on itself (a write to repository settings)
  or only refuses and tells the operator where to turn it on.

## Affected files

- `rlsbl/release_publication.py`: draft creation and a publish step.
- `rlsbl/commands/release/publish.py`: the upload flow and its error handling.
- `rlsbl/commands/undo.py`: behavior on immutable releases.
- `rlsbl/commands/release_reconcile.py`: the re-point verdict.
- The scrub command and its preflight.
- `rlsbl scaffold`, the release preflight, and the check registry, for
  enforcement.
- Tests: a red-green test per refusal, and one that performs each fix the
  refusal messages name.

## Effort

Step 1 with its tests: two to three days. Steps 2 to 4: one to two days each,
mostly in refusal paths and their tests. Step 5: a day. Step 6: an hour per
repository batch, done through rlsbl itself.
