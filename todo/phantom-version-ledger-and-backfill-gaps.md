# Phantom versions in the ledger, plus two backfill/message defects

Three related defects found while repairing a consumer repository's
pre-gate release ledger with `scripts/backfill_release_anchors.py`. The
repository in question has a permanently-cached phantom version: a tag that
reached the Go module proxy once by accident, was never a real release, and
can never be removed (proxy entries are immutable). Its ledger carried a
skeleton archive for that version from before the format-version gate.

## 1. The ledger has no concept of a never-released (phantom) version

The backfill script anchored the phantom's archive from its version-bump
commit, exactly as documented — and because the range anchor is the highest
archived version whose candidate commit is an ancestor, the phantom (a
major version above every real release) became the range anchor, making
hundreds of long-released commits report as unreleased. Marking the archive
`unanchorable = true` restored correct ranges, but the residue remains:

- `rlsbl status` reports the phantom as "Released: X (commit not
  recoverable)" — the absolute-highest-version rule has no way to say
  "this version is not a release".
- `rlsbl release run` refuses a checkout that does not contain the latest
  release; with the latest "release" unanchorable, containment is
  presumably indeterminable — the next real release from that repository
  may hard-error. Untested; if it does, the refusal message will not name
  the actual condition.

Proposed: a first-class marker on an archived release file (e.g.
`phantom = true` or `never_released = true`) that excludes the version
from "latest release", from the range anchor, from `unpublished-refs`
expectations, and from the contains-latest-release refusal — while keeping
the archive as the documented record of why the version number is burnt.
The marker is exactly the durable home for the "never create a 1.x tag"
class of accidents.

## 2. Backfill leaves pre-existing archives schema-invalid

The script stamps `format_version` onto pre-gate archives but does not
recover a missing mandatory `description` on archives that already exist —
it recovers descriptions only for archives it materializes. Result: after
a successful backfill run, every pre-description-era archive still fails
strictspec validation and every changelog-tagged check aborts. The
descriptions had to be recovered by hand from CHANGELOG.md. The script
should apply the same description-recovery chain (GitHub Release notes,
then CHANGELOG.md section, then a placeholder naming the recovery
obligation) to existing archives that lack the field.

## 3. Validation error suggests a command its dependency rejects

The release-file validation failure message says to run
`strictspec migrate --schema <name> --to 1 <paths>`, but strictspec's
`migrate` takes the schema as a positional FILE path and has no `--schema`
flag — the suggested command fails with "unknown flag". The message should
emit an invocation that actually runs (and ideally name where the schema
file ships).

## Effort

Item 1 is a small design plus touches to the ledger read paths and their
checks; items 2 and 3 are small fixes with red-green tests.
