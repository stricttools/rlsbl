# `release run --dry-run` renders an empty would-do body

## Context

Phase 6.2 adopted strictcli's effects regime. Most rlsbl commands now render a real
recorded preview under `--dry-run` (`claim-name`, `monorepo sync`, `monorepo snapshot`,
`dev sync`, `dev install`). `release run` — the deepest and most consequential command,
and the one the regime was designed to showcase — does not.

## Problem

`rlsbl release run --dry-run` exits 0 printing the engine's plan and an EMPTY would-do
body. The cause is architectural, not a wiring gap: the release engine gates every
mutation behind its own early `if dry-run: print_dry_run_summary(); return`, and
`_run_release_mutating` additionally hard-errors if it is ever reached in dry mode (the
structural fix from the resume-dry-run incident — worth keeping).

Removing that gate would not produce an end-to-end render either. The engine creates a
commit, then reads the SHA it just created, then waits for CI on that SHA — post-mutation
observes, which the contract requires to truncate. The preview would stop at the release
commit however the code is arranged.

## What a real preview would require

Restructuring so the post-mutation reads become either (a) forwarded carriers whose values
are never extracted, or (b) declared effect results the framework can thread. Concretely
the engine would need to stop branching on values it derives from its own mutations —
which is the same discipline the contract's `Unsettled` carriers already enforce
elsewhere, applied to the release flow's control structure.

## Options

- **(a) Restructure the mutating phase around carriers.** Pros: the flagship preview
  actually works; the release flow becomes the reference demonstration it was meant to be.
  Cons: the largest single refactor left in rlsbl; the CI wait is a genuine
  branch-on-mutated-state and may need a declared effect kind of its own.
- **(b) Keep the plan summary, drop the ambition.** Document that `release run --dry-run`
  previews the PLAN (versions, targets, steps) rather than the effect log, and that this is
  deliberate. Pros: honest, zero risk. Cons: the deepest command keeps the weakest preview,
  and the asymmetry will confuse anyone who reads the contract first.
- **(c) Hybrid: render up to the point of no return.** Preview every effect through the
  candidate push, then state plainly that the remainder depends on CI's verdict. Pros: most
  of the value, bounded work. Cons: a partial preview needs its own clearly-worded
  terminator so nobody reads it as complete.

## Affected files

`rlsbl/commands/release/execute.py` (the mutating phase), `rlsbl/commands/release/__init__.py`
(the dry-run gate), `rlsbl/commands/release/validate.py` (`print_dry_run_summary`),
`rlsbl/effects.py` (if the CI wait needs a declared kind).

## Effort

(a) large — a dedicated visit. (b) small. (c) medium.
