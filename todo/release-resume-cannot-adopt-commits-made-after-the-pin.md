# `release resume` cannot adopt commits made after the range pin

## What happened

A standalone Go project released with
`rlsbl release run --no-allow-dirty --watch --approve-consequential`.
The candidate was pushed and CI came back green:

```
rlsbl: candidate dc021db65133: [Docker] passed
rlsbl: candidate dc021db65133: [CI] passed
CI is green on dc021db65133
Error: Release aborted at the CI gate: commits that this release did not create
appeared on the branch after it started.

Foreign commits (not part of this release):
  c2eda12fc4ae  changelog: non-user-facing entry
  b59189072dac  todo: replace a banned term
  d7e1b8632f4c  changelog: non-user-facing entry
  996701bdd673  todo: export the git verb table as an importable package

Resolve one way or the other, then re-run:
  - to include them: record them with `rlsbl changelog add` and start a fresh release
  - to exclude them: move them off this branch (commit them on a branch of their own) first
```

Another session sharing the worktree had committed four commits (a new todo
file, a wording fix to it, and the two `changelog add` auto-commits that cover
them) during the CI wait. Every one of them was already covered:
`rlsbl unreleased` reported them `[COVERED]` / `[EXEMPT]`, and
`rlsbl check --tag changelog` passed clean.

Neither offered resolution was reachable:

- **Include them.** `rlsbl release run` refuses while the in-progress state
  exists: *"a previous release is in progress (v0.29.3, 5/16 steps completed
  ...). Run `rlsbl release resume` to continue or `rlsbl release undo` to roll
  back."* So a fresh release cannot be started.
- **Resume.** `rlsbl release resume --watch --approve-consequential` re-enters
  the flow and then hits the SAME guard at the mutating entry, with the same
  four commits and the same pin (`Pinned at 8d0e6c9d4675`). The state file's
  `pin_sha` is not updated by resume, so resume can never clear a pin violation
  that resume itself did not create.
- **Exclude them.** Moving another session's commits onto a branch of their own
  is not available in this environment: side branches are forbidden outright,
  and the commits are not this session's to move.

That leaves `rlsbl release undo` (consequential, and it rolls back a
CI-verified candidate that is already on origin/main) as the only remaining
move, which requires a human decision.

## Why this is more than one unlucky run

The same wall blocks the ordinary fix-forward loop. When a release fails after
the range pin is taken -- a red CI verdict, a `selfdoc check` finding, a failed
hook -- the prescribed recovery is: commit the fix, then
`rlsbl release resume`. But a fix commit made after the pin is, by the guard's
own definition, a commit "this release did not create". If the guard's
comparison treats it the way it treated the four commits above, resume refuses
the very commit it asked for.

The two readings, and they lead to different fixes:

1. **Resume is supposed to re-pin at the current tip and does not.** Under this
   reading the pin belongs to one `run` attempt, and every resume re-takes it,
   adopting whatever is on the branch -- which is what makes commit-the-fix-then-resume
   work at all. The fix is for resume to re-pin (and to say out loud which
   commits it is adopting, so the adoption is visible rather than silent).
2. **The pin is deliberately immutable for the life of a release** and resume is
   never meant to adopt anything. Under this reading the defect is that there
   is no sanctioned exit at all: `run` is blocked by the in-progress state,
   resume is blocked by the pin, and `undo` is the only door. Something has to
   exist that says "I have reviewed these commits, adopt them" or "abandon this
   release without rolling back the candidate" -- and `release run`'s refusal
   message should name it instead of naming "start a fresh release", which the
   in-progress state forbids.

Either way the message in the guard is wrong as printed: it prescribes a fresh
release that the in-progress state refuses.

## Affected surfaces

- The range-pin guard and its message (fired at both the mutating entry and the
  CI gate).
- `rlsbl release resume` and the in-progress state file's `pin_sha` /
  `release_created_commits` fields.
- `rlsbl release run`'s refusal when an in-progress release exists -- it points
  at resume and undo, while the pin guard points at a fresh run; one of the two
  is always a dead end.
- Whatever documents the recovery loop, since "commit the fix, then resume" is
  the flow at stake.

## Suggested tests

- A release that reaches the candidate push, then a commit appears on the
  branch from outside the release, then `resume`: assert the documented
  outcome, whichever reading wins.
- The fix-forward loop end to end: fail a release after the pin (a hook that
  exits non-zero once), commit a fix, `resume`, and assert it completes.
- The refusal messages: each remedy a message names is executed in a fixture
  and the error it promises to clear actually clears.

## Effort

Small if the answer is re-pinning on resume (state write plus the adoption
notice plus tests). Larger if a new consent surface is the answer, since that
is a new input and the message set has to be reworked with it.
