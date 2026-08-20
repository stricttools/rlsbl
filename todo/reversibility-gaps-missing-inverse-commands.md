# Reversibility gaps: commands whose inverse is missing

## Context

strictcli is gaining declared reversibility support: a mutating command will
declare which command undoes it (verified at registration in both
directions), a command with no recovery will declare irreversible with a
mandatory reason, a warn-severity check will flag destructive commands
declaring neither, and after a real run the framework will print a paste-able
recovery command and emit a machine-readable recovery member in the JSON
result document. When this repo adopts that support, every gap below needs
either a built inverse or an honest irreversible declaration.

## Problems

1. **`release deprecate` has no un-deprecate.** It sets a soft GitHub flag
   and prepends a notice — both trivially reversible operations with no
   command exposing the reversal.
2. **`release yank` has no un-yank.** The npm deprecation and the GitHub
   pre-release flag are both reversible on their platforms; the Go retract
   and PyPI legs are genuinely one-way and would stay declared irreversible.
3. **`changelog add` / `changelog amend` have no entry removal.**
   `changelog edit` can change an entry's fields but cannot delete an entry;
   a wrong `add` today is fixed by hand-editing JSONL.
4. **`dev sync` has no un-overlay.** `dev status` reports drift; nothing
   restores the locked registry wheels short of a manual re-sync.
5. **`monorepo init` and `monorepo migrate-releasable` have no reverse
   migration.**
6. **`deploy` has no undeploy.**

Also relevant to the declaration design: `rlsbl commit` is reversible via the
commit tool's own undo only when the preferred commit-tool backend actually
ran — the plain-git fallback leaves no undo record. The declared inverse will
need to express that condition, or the silent fallback (itself already filed
as a silent-degradation defect) goes first.

## Effort

Items 1-3 small (each is a thin command over existing platform calls or JSONL
mechanics; entry removal needs the same unlock/relock discipline amend has).
Items 4-6 medium. All can ride the repo's natural release after the framework
support ships.
