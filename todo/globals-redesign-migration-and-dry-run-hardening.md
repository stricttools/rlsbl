# Globals redesign migration and dry-run hardening

Filed 2026-07-22. Two bodies of work, dependency-ordered: an INTERIM hardening pass that is
releasable immediately and independent of everything else, and the MIGRATION to strictcli's
adopted globals redesign ("design A": framework-reserved flags, effect typing, effects
handles) once that framework work lands. All decisions below are LOCKED — made explicitly by
the user on 2026-07-22. Nothing is open for an implementor to redecide.

## Context

Incident (2026-07-22): `rlsbl claim-name <name> --target npm --dry-run` ACTUALLY PUBLISHED a
placeholder package to npm — the handler received `dry_run` into `**_kwargs` and dropped it,
and no confirmation prompt existed on the npm/pypi paths. Fixed same day (unreleased):
handler sweep naming all globals, claim-name dry-run gate + confirmation prompts, `push
--dry-run`, undo flags-key normalization — audited, full suite green (7719 passed). NOTE:
the released version 0.109.1 still carries the live publish-on-dry-run bug, which is why the
interim phase below should release soon, ahead of the weeks-scale framework redesign.

A census of all 52 rlsbl commands during the design round found dry-run honored by 24
commands, `--yes` by 13, `--quiet` by 5 — and TWELVE mutating commands that accept
`--dry-run` today and silently execute for real (the danger 12): `commit`, `record-gif`,
`release init`, `monorepo init`, `monorepo add`, `monorepo remove`, `monorepo sync`,
`monorepo snapshot`, `monorepo mirror` (worst: pushes to a git remote), `monorepo release
init`, `dev install`, `dev sync`.

## Part 1 — interim hardening (independent; do first; releasable on user trigger)

1. **Clean non-TTY prompt failure.** The confirmation prompts added to claim-name
   (rlsbl/commands/claim_name.py:117 and the crates permanence prompt at :104) and push
   (rlsbl/commands/push_cmd.py:207) call bare `input()`: on non-interactive stdin they die
   with an uncaught EOFError traceback (they fail SAFE — nothing publishes — but ugly).
   Wrap all three in try/except (EOFError, KeyboardInterrupt) and fail with a clean hard
   error naming the remediation: "stdin is not interactive; pass --yes to confirm", exit 1.
   Also upgrade the existing catch blocks in deprecate.py (:115) and undo.py (:685, :736) to
   include the same --yes remediation hint. Red-green: tests simulating EOF on stdin first.
2. **push --dry-run delegates to `git push --dry-run`.** Decision: a dry run should be a
   true rehearsal. Keep all local preflights (branch guard, changelog coverage,
   behind-remote), then instead of only printing "would push", additionally run
   `git push --dry-run` so the remote validates the ref update (rejections, permissions,
   non-fast-forward). A green dry run must mean the real push would succeed. Requires
   network like the real push — accepted. Red-green.
3. Changelog entries per item (`rlsbl changelog add`); the push change is user-facing
   (amends the just-added push --dry-run feature entry or adds a fix-type entry); the prompt
   fix is user-facing (fix). Release only on explicit user trigger, via the standard flow.

Decision already locked and implemented, recorded for completeness: dry runs REQUIRE the
registry token (identical preconditions to a real run — a dry run without NPM_TOKEN fails
the same way a real run does). Do not "improve" this into a credential-free preview.

## Part 2 — migration to design A (blocked on strictcli landing the redesign)

strictcli's adopted redesign, as it affects rlsbl: `--dry-run`/`--yes`/`--quiet` become
FRAMEWORK-RESERVED flags with app-level opt-in switches (apps stop declaring them);
commands declare `read_only` or `mutating` (absence = gates rejected, fail-safe); the
framework owns the y/N confirmation before mutating commands (with the clean non-TTY error)
and honors quiet at the Context output layer; dry-run honoring flows through injected
`ctx.effects` handles that record instead of performing in dry mode; a lint check flags
direct subprocess/socket use in handlers. Guard v1 ("name every global") is replaced by v2
("name exactly what you support") and never ships — consumers see ONE breaking change.

Migration items:

1. **Adopt reserved flags.** Delete rlsbl's three app-level global declarations
   (rlsbl/__init__.py:158-162); enable the framework opt-in switches instead.
2. **Un-sweep handler signatures.** The 2026-07-22 sweep added dry_run/yes/quiet parameters
   to every handler; under design A, `yes`/`quiet` parameters disappear entirely
   (framework-owned) and `dry_run` kwargs disappear in favor of effects-handle behavior.
   Collapse this into the same pass as items 3-6 — do not touch handlers twice.
3. **Classify all 52 commands** read_only/mutating. The census classification exists in the
   design record; the mutating set is the 24 dry-run-honoring commands plus the danger 12
   plus push/deploy-class commands; everything else read_only. read_only commands MAY keep
   framework-blessed internal cache writes (e.g. the changelog validation cache) — locked
   policy: read_only means no user-visible/consequential mutation, not zero writes.
4. **Split two straddling commands.** `watch`: public command becomes honestly read_only;
   the `--as-daemon-child` plumbing (spawns a detached process, writes a pidfile; used by
   --watch-async) moves to a hidden internal command classified mutating. `monorepo
   snapshot`: split into a mutating snapshot command (writes snapshot.json + commits; gains
   real dry-run) and a read_only check command (absorbing today's `--check` mode). Both are
   breaking CLI changes; changelog them as such.
5. **Route mutating commands through `ctx.effects`.** All subprocess/git/filesystem side
   effects in mutating handlers go through the injected handles (the `monorepo mirror` git
   push becomes an effects call, automatically inert in dry mode). Delete the hand-rolled
   confirmation prompts (framework owns them now) including the Part-1 code — Part 1 is a
   stopgap for the interim release and is consciously superseded here.
6. **Real dry-run for the danger 12.** With effects routing, each of the twelve gets a
   genuine dry mode (recorded would-do output, nothing performed). Red-green per command:
   a failing test that the dry invocation performs no side effect, then the routing, then
   green. `claim-name`'s existing behavior (token still required in dry runs) is preserved.
7. **Tests and changelog.** Full suite green; per-item changelog entries; the sweep-era
   regression test asserting guard-v1 compliance gets rewritten for the v2/design-A model.

## Sequencing

Part 1 has no dependencies — implement immediately; release on user trigger. Part 2 is
blocked on the strictcli redesign (contract → Python reference → Go/TS parity) landing in
the editable install; within Part 2, items 1-6 are ONE pass over the handler surface
(collapse, never sequential sweeps), item 7 continuous. After Part 2: fresh spec-only audit,
then release on user trigger, after strictcli's breaking release.

## Affected files

- rlsbl/__init__.py — global declarations (:158-162), all 52 handler registrations/
  signatures, watch + snapshot command definitions.
- rlsbl/commands/claim_name.py, push_cmd.py, deprecate.py, undo.py — Part 1 prompt/dry-run
  changes; later prompt deletion under Part 2.
- rlsbl/commands/* (mutating set) — effects routing, danger-12 dry-run implementations.
- tests/ — non-TTY prompt tests, push git-dry-run test, per-command dry-run suppression
  tests, guard regression test rewrite.
- .rlsbl/changes/unreleased.jsonl — entries per item.

## Effort estimate

Part 1: hours. Part 2: roughly a week after the framework lands — 52 registration sites,
one collapsed handler pass, twelve red-green dry-run implementations, two command splits.
