# CLI-surface release gate: block under-bumped surface changes

Filed 2026-08-03. Design decisions below were made deliberately; the upstream surface-diff/classifier tool is planned in the CLI framework's repo and produces a graded certificate this gate consumes.

## Context

For CLI-framework-based projects, `.strictcli/schema.json` describes the complete CLI surface and is regenerated + version-stamped during release (`commands/release/__init__.py:844`). The previous release's schema is reliably retrievable via `git show <last_tag>:./.strictcli/schema.json` (verified; use the `:./` pathspec form per the note at `commands/init_cmd.py:576-578`, tag glob via `checks/_common.py:153`, last tag via `utils.py:157`). Diffing outgoing-vs-last and classifying the delta (breaking/feature/fix) makes bump-type enforcement mechanical.

## Decided design

- **Shape: direct pre-mutation guard**, beside `_abort_on_version_skew` (`commands/release/validate.py:874`), NOT (only) a preflight-tag check — a customized pre-release hook silently skips built-in preflight checks (`__init__.py:1018-1043`, `:930-953`), and the existing strictspec certificate gate has that hole today. Optionally add a thin observability check that reports without gating.
- **Bump-type plumbing**: add `bump_type: str | None` to `ProjectContext` (`rlsbl/context.py:13`), populated only by the release flow. Unblocks any future bump-aware check.
- **Rules**: breaking surface delta ⇒ minor+ in 0.x (major in 1.x+). Additive delta (new command/flag, widened choices) ⇒ minor+. Default-value changes are graded breaking by the upstream classifier. `infra` bump with ANY surface delta ⇒ error (infra already forbids user-facing entries). First release (`bump_type` None, `validate.py:492`) ⇒ skip. `prerelease` semantics resolved during design.
- **Rollout: automatic** for every project where CLI-framework detection succeeds — no opt-in config, no escape hatch. Resolution path for an intended breaking change is changing the bump in the release file (read fresh every run, `release_file.py:378`, strictspec-validated) or the `--bump` flag in quick-bump mode.
- **Binary requirement**: the diff/classifier binary is required when the project is CLI-framework-based; absence is a hard error with an install hint — same contract as the gitleaks secret-scan dependency. Subprocess + certificate file; the gate never computes verdicts itself.

## Mechanics the implementation must handle (all verified in code)

- **Releasable mode dumps only the representative member's schema** (`__init__.py:844`) while preflight runs per member — loop the dump over `member_package_paths` or gate at releasable level with per-member attribution. The representative-only dump is a bug independent of this feature.
- **Resume idempotence** (`__init__.py:176` re-enters pre-mutation): never compare a bumped working-tree version against the tag as if it were a delta.
- **Dry-run cannot evaluate the gate** (no schema dumped under `--dry-run`, `validate.py:1056-1061`; impure checks listed not run under `pure_only`) — document, and consider a standalone preview command.
- **Remediation text branches by invocation mode**: release-file edits vs `--bump` re-run (quick-bump refuses coexisting release files, `rlsbl/__init__.py:319-325`).
- Shallow clone ⇒ existing hard error from `get_last_version_tag` (`utils.py:186-190`). Tag resolvable but schema unreadable ⇒ hard error (configured-must-work). No schema at last tag ⇒ skip (newly CLI-fied project), reported distinctly from "no changes".
- Normalize the stamped `version` key out before diffing; branch explicitly on `schema_version` mismatch between baseline and outgoing.
- Tag schemes: standalone `v*`, releasable `name@v*`, Go path-style — all via the existing glob resolution (`tag_glob.py:61-94`).

## Attachment

The graded delta is rendered into the changelog via the generic contribution protocol (`todo/changelog-contribution-protocol.md`) — this gate only gates.

## Affected files

`rlsbl/commands/release/__init__.py` (guard call site), new guard module beside `rlsbl/strictspec_gate.py` pattern, `rlsbl/context.py`, `rlsbl/data/checks.toml` + `checks/__init__.py:46` if the observability check is added, `selfdoc gen` re-run for the check-count directive.

## Dependencies

Hard: stamping correctness (`todo/schema-stamping-correctness.md`); the upstream classifier tool existing. Soft: contribution protocol for changelog rendering.

## Effort

M-L.
