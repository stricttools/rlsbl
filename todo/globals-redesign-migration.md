# Globals redesign migration (design A adoption)

Filed 2026-07-22, split from a combined todo the same day; the companion file
`todo/interim-dry-run-hardening.md` holds the independent pre-redesign work. This part is
BLOCKED on strictcli landing its globals redesign ("design A": framework-reserved flags,
effect typing, effects handles). All decisions are LOCKED — made explicitly by the user on
2026-07-22. Nothing is open for an implementor to redecide.

## Context

Incident (2026-07-22): `rlsbl claim-name <name> --target npm --dry-run` ACTUALLY PUBLISHED a
placeholder package to npm — the handler received `dry_run` into `**_kwargs` and dropped it.
The same-day fixes and the strictcli guard ("name every global") were stopgaps; the census
below motivated a structural redesign in strictcli, which this todo adopts for rlsbl.

Census of all 52 rlsbl commands (2026-07-22 design round): dry-run honored by 24 commands,
`--yes` by 13, `--quiet` by 5 — and TWELVE mutating commands that accept `--dry-run` today
and silently execute for real (the danger 12): `commit`, `record-gif`, `release init`,
`monorepo init`, `monorepo add`, `monorepo remove`, `monorepo sync`, `monorepo snapshot`,
`monorepo mirror` (worst: pushes to a git remote), `monorepo release init`, `dev install`,
`dev sync`.

## The strictcli redesign, as it affects rlsbl

`--dry-run`/`--yes`/`--quiet` become FRAMEWORK-RESERVED flags with app-level opt-in switches
(apps stop declaring them); commands declare `read_only` or `mutating` (absence = gates
rejected, fail-safe); the framework owns the y/N confirmation before mutating commands (with
a clean non-TTY error) and honors quiet at the Context output layer; dry-run honoring flows
through injected `ctx.effects` handles that record instead of performing in dry mode; a lint
check flags direct subprocess/socket use in handlers. Guard v1 ("name every global") is
replaced by v2 ("name exactly what you support") and never ships — consumers see ONE
breaking change.

## Migration items

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
   confirmation prompts (framework owns them now), including any interim non-TTY stopgap
   from the companion todo — that code is consciously superseded here.
6. **Real dry-run for the danger 12.** With effects routing, each of the twelve gets a
   genuine dry mode (recorded would-do output, nothing performed). Red-green per command:
   a failing test that the dry invocation performs no side effect, then the routing, then
   green. `claim-name`'s existing behavior (token still required in dry runs) is preserved.
   The `git push --dry-run` delegation from the companion todo survives: the push effect's
   dry mode keeps the remote-validating rehearsal semantics.
7. **Tests and changelog.** Full suite green; per-item changelog entries; the sweep-era
   regression test asserting guard-v1 compliance gets rewritten for the v2/design-A model.

## Sequencing

Blocked on the strictcli redesign (contract → Python reference → Go/TS parity) landing in
the editable install. Within this todo, items 1-6 are ONE pass over the handler surface
(collapse, never sequential sweeps), item 7 continuous. Afterward: fresh spec-only audit,
then release on user trigger, after strictcli's breaking release.

## Affected files

- rlsbl/__init__.py — global declarations (:158-162), all 52 handler registrations/
  signatures, watch + snapshot command definitions.
- rlsbl/commands/* (mutating set) — effects routing, danger-12 dry-run implementations,
  prompt deletion (claim_name.py, push_cmd.py, deprecate.py, undo.py, scrub, yank).
- tests/ — per-command dry-run suppression tests, guard regression test rewrite.
- .rlsbl/changes/unreleased.jsonl — entries per item.

## Effort estimate

Roughly a week after the framework lands — 52 registration sites, one collapsed handler
pass, twelve red-green dry-run implementations, two command splits.
