# The release flow's documentation-tool subprocesses now need `--yes`

## Context

The release flow shells out to an external documentation generator at three
points: a `gen` step and a `check` step during the preflight, and a `deploy`
step in the Cloudflare Pages pipeline. All three run as subprocesses of the
release run, which has no interactive stdin in any of its real contexts (CI, a
release script, an agent session).

That documentation tool has now migrated onto the released strictcli effects
regime. Its `gen`, `check` and `deploy` commands are classified `mutating` --
correctly: `gen` writes generated pages and read-only root files, `check`
rewrites the content-hash staleness baseline, and `deploy` force-pushes a
`gh-pages` branch or ships a Cloudflare Pages deployment.

## Problem

strictcli's confirm protocol fires before dispatching any `mutating` command on
the real CLI path unless `--dry-run` or `--yes` was passed, and when stdin is
not a TTY it does not prompt -- it hard-errors:

```
error: stdin is not interactive; pass --yes to confirm
```

There is no opt-in and no app-level switch; the protocol is unconditional for
mutating commands. So every one of those three subprocess invocations now exits
1 before doing anything, and the release preflight fails at the docs step.

The same class of breakage is not hypothetical: the consumer's own suite caught
it from the other direction. A test that exercises the real installed commit
chain went red with exactly this message, because the release tool's own
`commit` is likewise a mutating strictcli command and the consumer was invoking
it without `--yes`. That side has been fixed in the consumer by passing `--yes`
to the internal commit invocation.

## Fix

Add `--yes` to each documentation-tool invocation in the release flow. They are
tool-driven, fully determined subprocesses of a run the user already initiated
(and, for a release, already confirmed): there is no second decision for the
child to confirm, and no interactive context in which the prompt could be
answered.

## Affected sites

- the preflight's `gen` invocation (currently `<tool> gen --no-auto-commit`)
- the preflight's `check` invocation (currently `<tool> check --no-auto-commit`)
- the Cloudflare Pages pipeline's `deploy` invocation

Worth sweeping for the same shape more broadly: any subprocess invocation of
another strictcli app's mutating command made from a non-interactive context
needs the same treatment, and the failure mode is a hard exit rather than a
hang, so it will show up as an opaque "stdin is not interactive" in whatever
step wraps it.

A regression test that runs the affected step with stdin closed and asserts it
completes would keep this from recurring as more consumers migrate.

## Effort

Small -- three argument additions plus the sweep and one regression test.
