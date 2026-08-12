# Tiered classification for the observe allowlist (deferred)

## Status

Deferred deliberately. The observe allowlist now carries a single, flat
standard -- "no user-visible mutation" -- written down in
`rlsbl/observe_allowlist.py` and machine-checked by
`tests/test_observe_allowlist.py`. Every entry declares one of three
categories (`local-read`, `network-read`, `self-report`) and a reason. That is
what shipped, and it is enough for every entry currently on the list.

This file records the alternative that was considered and set aside, so a
future session does not re-derive it from scratch.

## The alternative

Instead of one admit/refuse standard, grade the allowlist into tiers and let
each tier carry different consequences at preview time. The shape that was
discussed:

| Tier | Meaning | Preview consequence |
| --- | --- | --- |
| pure read | touches only local state, writes nothing at all | executes silently, as today |
| cache-touching read | reads a remote, writes only the tool's own cache | executes, but the preview names the cache it warmed |
| stateful read | reads something whose act of reading changes observable state (a rate-limit counter, an audit log entry on the far side) | executes only when the command opts in, and the preview says so |

The appeal: the flat standard currently answers "may this run under a preview?"
with one bit, and that bit is doing several jobs at once. `git rev-parse` and
`npm view` are both "yes", but they are not the same kind of yes -- one is
inert, the other reaches a registry and leaves a downloaded artifact behind.
Tiers would make that difference visible in the would-do log instead of only
in a comment.

## Why it was deferred

- **Nothing on the current list needs it.** All 53 entries fit one of the three
  flat categories cleanly. A tier system with no entry that distinguishes the
  tiers is machinery paid for in advance.
- **The consequence half is the expensive half.** Tiers are only worth having
  if the framework does something different per tier, and the deciding code
  lives in strictcli's effects handle, not in rlsbl. That is a framework
  contract change, not a consumer-side list refactor.
- **The flat standard already refuses the real hazards.** Ref updates, index
  writes and credential emission are the three things that actually bit, and
  the flat standard bans all three by name with a test corpus behind it.

## What would make this live again

Any of:

- An entry that genuinely belongs on the list but whose read is observable on
  the far side (a registry endpoint that counts reads against a quota the
  release later depends on, an API whose read writes an audit record).
- The framework growing per-effect annotations in the preview log, so a tier
  would have somewhere to be rendered.
- A second consumer of the allowlist that needs to reason about entries rather
  than merely match them.

## Affected files, if resumed

- `rlsbl/observe_allowlist.py` -- the categories and entries.
- `tests/test_observe_allowlist.py` -- the shape assertions and the ban corpus.
- strictcli's effects handle -- the per-tier consequence, which does not exist.

## Effort estimate

Small in rlsbl (half a day: the entries are already categorized). Unbounded
until the framework side is designed, which is why it is here and not in
`todo/`.
