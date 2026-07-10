# monorepo release init: race re-blanks a filled release TOML

## Context

Observed during a real monorepo release. The operator ran `rlsbl monorepo release init`,
edited `.rlsbl-monorepo/releases/unreleased.toml` (set bump + description), committed it,
and proceeded toward `rlsbl monorepo release run`.

## Problem

The init invocation appeared to auto-background (the command returned while work was
still pending), and a SECOND, late-completing init pass re-scaffolded the release file —
overwriting the already-filled TOML back to the blank template. Evidence: two consecutive
`release: scaffold` commits in the repo's history for one logical init, with the filled
values lost between them. The operator noticed only because the subsequent release run
would have failed on the blank `bump = ""`; they re-filled and re-committed by hand.

A release file silently reverting to template defaults is dangerous: `bump` and
`description` are the release's core inputs, and "blank" is caught by validation only if
the operator re-runs; a half-blank file (e.g. description kept, bump lost) could plausibly
slip through as the wrong release type.

Reproduction is not pinned down — it occurred once (heavy-load environment, multiple
agents/sessions active) and did not recur on a later release in the same repo. Treat the
mechanism below as a hypothesis to verify, not a confirmed diagnosis.

## Investigation starting points

1. Does `monorepo release init` spawn any background/deferred work (async scaffold,
   delayed file write, watcher)? If it is strictly synchronous, the second scaffold must
   have come from a second invocation — check whether init is invoked indirectly anywhere
   (e.g. by `release run` preflight, a hook, or shell retry) that could double-fire.
2. Whatever the trigger: init should be **idempotent-and-preserving** — if
   `unreleased.toml` exists and differs from the template (operator-filled), init must
   NOT overwrite it (hard error or no-op with a message), matching the ecosystem's
   "scaffold never overwrites user-owned files" convention.

## Proposed fixes

1. **Make init refuse to overwrite a non-template release file (recommended).** If the
   existing file's content differs from the pristine template, print "release file
   already prepared; not overwriting" and exit 0 (or hard-error with `--force-overwrite`
   absent — but note bare `--force` is banned; a qualified flag or simply refusing is
   cleaner). This makes the race harmless regardless of its trigger.
   - Pros: converts a data-loss race into a no-op; defense independent of root cause.
   - Cons: none meaningful.
2. **Find and remove the double-fire** (root cause), guided by the investigation above.
3. Both: 1 is the guardrail, 2 the cure.

## Affected files

- The monorepo release-init command implementation (and any call sites that re-invoke it).
- A red-green test: init once → fill the TOML → init again → assert the filled content
  survives.

## Effort

Small. Guardrail + test ~1 hour; root-cause hunt unbounded but bounded by the guardrail
making it low-stakes.
