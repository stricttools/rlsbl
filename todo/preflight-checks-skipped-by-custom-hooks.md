# Built-in preflight checks are silently skipped when the pre-release hook is customized

Filed 2026-08-03.

## Problem

When a project's `pre-release.sh` hook is customized, the release flow routes to `run_external_preflight_checks` ONLY — built-in preflight-tag checks are skipped entirely (`commands/release/__init__.py:1018-1043` standalone, `:930-953` releasable). This silently disables enforcement checks, including the `strictspec-certificate-gate` check (`data/checks.toml`, tags `project` + `preflight`, severity error): a project with a customized hook can release with a `violated` certificate claim and no error. A silent skip of an error-severity check contradicts the hard-error philosophy — the skip is invisible to the operator.

`docs/release-workflow.md:186-191` documents that direct validation calls (the `_abort_on_version_skew` style) are immune to this skip, which is why the planned CLI-surface guard was designed as a direct call — but the existing certificate check and any other error-severity preflight check remain exposed.

## Options

- **A. Always run built-in preflight checks, additionally to external checks, regardless of hook customization (recommended).** Investigate why customization skips built-ins at all — if the original rationale was avoiding duplicate work when hooks re-run checks, the correct shape is built-ins always + externals always, deduplicated by name.
  - Pros: closes the hole for every current and future preflight check at once; no per-check migration.
  - Cons: changes behavior for every project with a customized hook (they suddenly get enforcement they were silently missing — which is the point, but may surface latent failures fleet-wide on next release).
- **B. Migrate error-severity enforcement checks to direct pre-mutation calls** beside `_abort_on_version_skew` (`commands/release/validate.py:874`), leaving informational checks in the skippable preflight set.
  - Pros: matches the pattern already chosen for the planned CLI-surface guard; explicit two-tier model (enforcement vs diagnostics).
  - Cons: per-check migration; two mechanisms to maintain; the skippable tier remains a trap for future error-severity checks added to the wrong tier.
- **C. Keep the skip but hard-error when the skipped set contains error-severity checks.** Rejected direction: it converts the hole into a permanent obstacle for customized-hook projects instead of running the checks.

## Affected files

`rlsbl/commands/release/__init__.py` (both routing branches), `docs/release-workflow.md` (documented skip semantics change), tests covering customized-hook release paths.

## Effort

S-M. Independent of other planned release-flow work but should land before or with any new enforcement checks that use the preflight tag.
