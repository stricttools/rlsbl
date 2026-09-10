# A committed bad-versions record, and a check that registries respect it

## Context

Knowledge that a shipped version is bad currently lives in two places,
neither of which is a record: prose (a changelog section opening with "the
previous version shipped with known defects" while that version remains
fully installable from every registry) and actions (`rlsbl release yank`
performs registry-specific withdrawal but leaves no committed declaration
of which versions are condemned or why). There is no single authority a
tool or a human can read, and nothing verifies that registry reality
matches anyone's intent.

rlsbl already has the right pattern for exactly this: the release archives
are a committed declarative record, and fail-closed checks
(`unpublished-refs`, `release reconcile --plan`) render the record against
the world and report divergence with named remedies. Bad versions deserve
the same treatment.

## The invariant

A version is available online if and only if it was released and is not
bad. Both directions are violations:

- a bad version still fully available: withdrawal was never performed, or
  was performed on some targets and not others;
- a released, non-bad version missing or withdrawn: a registry incident or
  an out-of-band withdrawal nobody recorded;
- an online version with no release archive at all: the phantom class (a
  tag that reached a registry without ever being released). The same check
  surfaces it as operator input, the way the anchor backfill treats a tag
  matching no released version — never guessed at.

## "Not online" must translate per target

Literal removal is impossible or restricted on most registries, so badness
maps to each target's strongest sanctioned withdrawal state, and the check
verifies that state rather than absence:

| Target | Bad version must be | Non-bad version must be |
| --- | --- | --- |
| PyPI | yanked (PEP 592: excluded from resolution, exact pins still install) | present and not yanked |
| npm | deprecated with a message (unpublish is policy-restricted) | present and not deprecated |
| Go proxy | covered by a published `retract` directive (the proxy cache is permanent) | resolvable and not retracted |
| GitHub Release | marked pre-release with a withdrawal notice (never deleted, matching the rewrite-in-place principle) | present, not so marked |

The table is the design's honest core: "offline" is a state machine per
registry, not a boolean, and the check must encode it fail-closed (a probe
that cannot answer is a hard error, never a pass).

## Design

1. **The record.** One committed file per project (per releasable in
   monorepos, beside the release state), listing each bad version with a
   mandatory reason and optionally the version that supersedes it. The
   file is the single authority; nothing else restates the list. Validation:
   every listed version must have a release archive (released, or recorded
   as a phantom the way archives already record never-released versions).
2. **The check.** Release-tagged (network, fail-closed, out of preflight
   like the other remote-facing checks). For every configured target it
   probes every archived version's availability/withdrawal state and
   reports both violation directions plus phantoms, each naming its
   remedy (`rlsbl release yank <v>` for an unenforced bad version; the
   retraction procedure for a Go phantom; operator input for the rest).
3. **The write path.** Declaring a version bad is file-driven with a
   mandatory reason, consistent with the effects regime. `rlsbl release
   yank` then becomes the enforcement arm of the record rather than a
   freestanding action: it reads the reason from the record, and yanking a
   version with no record entry is refused (action without declaration is
   the current defect). `release deprecate` stays the soft, non-condemning
   flag it is today.
4. **Prose derived, not duplicated.** CHANGELOG.md generation annotates
   bad versions' sections from the record (version heading carries the
   withdrawal and reason), so the changelog prose that motivated this todo
   becomes derived output of the single authority instead of the only
   place the knowledge lives.

## Open decisions (deliberately not made here)

- File name/location and the command-surface spellings.
- Whether declaring a version bad should offer to run the enforcement
  (yank) in the same flow or always leave it to the check's remedy.
- Whether the availability direction for non-bad versions should be error
  or warn severity at first (registry flakiness vs. real incidents).
- Interaction with `release undo`: undo is for releases that provably
  never published and erases the record; bad-versions is for releases that
  shipped and must stay recorded forever. The boundary should be stated in
  both commands' help.

## Affected area

The check registry (a new release-tagged check), `release yank` (record
integration and refusal), changelog generation (withdrawal annotations),
release-archive validation (phantom entries), scaffold (the record file's
home), docs.

## Effort

Medium: the per-target probe/withdrawal-state matrix is the bulk (each
target answers availability and withdrawal differently); the record file,
validation, and changelog annotation are small; the yank integration is a
behavioral change needing its own red-green tests per the remedy-
truthfulness rule (the check's named remedies must be executed in fixtures
and shown to clear the violation).
