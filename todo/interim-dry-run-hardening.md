# Interim dry-run hardening (pre-redesign, releasable)

Filed 2026-07-22, split from a combined todo the same day; the companion file
`todo/globals-redesign-migration.md` holds the later migration work. This part is
independent, has no dependencies, and exists to make rlsbl releasable AHEAD of the
weeks-scale strictcli globals redesign. All decisions are LOCKED — made explicitly by the
user on 2026-07-22.

## Context

Incident (2026-07-22): `rlsbl claim-name <name> --target npm --dry-run` ACTUALLY PUBLISHED a
placeholder package to npm — the handler received `dry_run` into `**_kwargs` and dropped it,
and no confirmation prompt existed on the npm/pypi paths. Fixed same day (unreleased):
handler sweep naming all globals, claim-name dry-run gate + confirmation prompts, `push
--dry-run`, undo flags-key normalization — audited, full suite green (7719 passed). NOTE:
the released version 0.109.1 still carries the live publish-on-dry-run bug, which is why
this interim phase should release soon.

## Work items

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

## Sequencing and value contingency

No dependencies; can run in parallel with the strictcli redesign. IMPORTANT: item 1's value
is contingent on an interim rlsbl release happening BEFORE the globals-redesign migration
lands — the migration deletes hand-rolled prompt code entirely (framework-owned prompting
supersedes it), so without an interim release the prompt fix is throwaway. Item 2 (`git
push --dry-run` semantics) survives the migration. If no interim release is wanted, skip
item 1 and keep only item 2.

## Affected files

- rlsbl/commands/claim_name.py, push_cmd.py, deprecate.py, undo.py
- tests/ — non-TTY prompt tests, push git-dry-run test
- .rlsbl/changes/unreleased.jsonl — entries per item

## Effort estimate

Hours.
