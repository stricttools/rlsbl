# Changelog stamper cannot handle mixed-state `unreleased.jsonl`

## Context

The changelog JSONL format gained a per-line `format_version` field. To roll this
out across existing repos, there is a one-shot stamping tool that walks every
`.jsonl` file under a project's changes directory and writes `format_version`
into each line that lacks it. This is the fleet bootstrap: stamp all historical
lines once, then flip on `format_version` enforcement.

The stamper was designed for the clean case — a file whose lines are ALL
unstamped — and it treats the presence of any already-stamped line as a signal
that the file was already processed, so it refuses to touch the file at all.

## Problem

`unreleased.jsonl` is not a clean case. Any repo that ran `rlsbl changelog add`
AFTER upgrading to the stamping-aware `changelog add` (which auto-stamps each NEW
line it appends) now has a MIXED file: newly added lines carry `format_version`,
while historical lines written before the upgrade do not.

When the one-shot stamper hits such a mixed `unreleased.jsonl`, it sees at least
one already-stamped line, concludes the whole file is done, and aborts with ZERO
mutations. The unstamped historical lines are left unstamped. Enforcement then
fails on those lines, and the bootstrap cannot complete for that repo through the
intended path.

The observed workaround was a targeted partial-stamp: reuse the stamper's own
per-line primitives to stamp ONLY the unstamped lines in the mixed file, leaving
the already-stamped lines byte-identical. This proves the fix is small and that
the primitives already exist — they are just gated behind an all-or-nothing
file-level guard.

## Root cause

The stamper's idempotency guard operates at FILE granularity ("this file has a
stamped line, therefore skip") instead of LINE granularity ("stamp the lines that
lack the field, leave the rest untouched"). The file-level guard was a
conservative choice for the clean bootstrap but is wrong for any file that
legitimately mixes stamped and unstamped lines — which is the normal state of
`unreleased.jsonl` in any actively developed repo mid-rollout.

## Solutions

### Option A — Documented `--partial` mode (stamp only unstamped lines)

Add an explicit `--partial` flag that switches the guard from file-level to
line-level: iterate lines, stamp the ones missing `format_version`, leave stamped
lines exactly as-is, and write the file only if at least one line changed.
Default (no flag) keeps the strict all-or-nothing behavior so the clean-bootstrap
contract is unchanged and the agent must consciously opt into partial stamping.

- Pros: preserves the strict default; makes the mixed-file case a first-class,
  named operation; reuses the existing per-line primitives; matches the
  "mandatory explicit choice over silent behavior" philosophy — the caller
  declares intent.
- Cons: one more flag to document and test.

### Option B — Always operate at line granularity (drop the file-level guard)

Make the stamper unconditionally line-level: every run stamps only unstamped
lines and is naturally idempotent (a fully stamped file is a no-op).

- Pros: no flag; the mixed case "just works"; simplest mental model.
- Cons: silently changes the tool's contract; loses the explicit signal that a
  file was expected to be entirely unstamped; a partially-processed file no longer
  surfaces as an anomaly. Weaker fit for the "make the agent choose explicitly"
  philosophy.

### Option C — Hard error on mixed files with remediation hint

Keep the strict guard but, on detecting a mixed file, fail loudly with a message
pointing at `--partial` instead of silently aborting with zero mutations.

- Pros: no more silent no-op; the failure is actionable.
- Cons: incomplete on its own — needs Option A (or B) to actually resolve the
  mixed file. Best treated as a complement to A, not an alternative.

## Recommendation

Option A plus the Option C loud-failure behavior: strict by default, hard error
(never a silent zero-mutation abort) when a mixed file is seen without `--partial`,
and a documented `--partial` mode that stamps only unstamped lines using the
existing primitives. This keeps the clean-bootstrap contract intact, kills the
silent no-op, and gives the mixed case a named, explicit path.

## Tests (red-green)

- RED: a mixed `unreleased.jsonl` (some stamped, some unstamped lines) run through
  the default stamper — assert it currently aborts with zero mutations (reproduce
  the bug), then assert the new behavior is a hard error with a `--partial` hint.
- GREEN: the same mixed file run with `--partial` — assert every previously
  unstamped line gains `format_version`, every previously stamped line is
  byte-identical, and a fully-stamped file is a no-op (idempotent second run).
- Guard: a fully-unstamped file still stamps cleanly under the strict default
  (existing contract unbroken).

## Affected files

- The one-shot format_version stamping tool/script and its per-line stamping
  primitives.
- The stamper's test suite (add the mixed-state red-green cases above).
- Stamper documentation / help text (document `--partial` and the strict default).

## Effort

Small. The per-line primitives already exist (the observed workaround used them);
the change is relocating the idempotency guard from file to line granularity
behind an explicit flag, turning the silent abort into a hard error, and adding
the red-green tests. Roughly a couple of hours including tests and docs.
