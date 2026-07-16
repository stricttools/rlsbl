# external_checks: two bugs make config-declared external checks silently dead

## Context

A Go project declared an `external_checks` entry in `.rlsbl/config.json` (per the documented schema; the config validates cleanly) to keep an expensive verification in the release path. Investigation of why it never executed found two independent bugs.

## Bug 1: `rlsbl check` never sees config-declared external checks

strictcli's check handler filters/selects checks (`_filter_checks`) BEFORE invoking the check-context factory, but rlsbl registers external checks from config INSIDE that factory (`rlsbl/__init__.py:_check_context_factory` → `rlsbl/external_checks.py`). Consequence: the external check appears in no `rlsbl check --all`, `--tag`, `--list`, or `--name` output — it is silently absent, the exact opposite of the hard-error philosophy. Registration must happen before filtering (or the factory must run before selection).

## Bug 2: release preflight skips external checks when `hooks.pre_release` is customized

In `rlsbl/commands/release/__init__.py` (~line 826), a `hook_is_customized` condition short-circuits a preflight block that includes `_register_external_checks_from_config` and `run_checks(tag_expr="preflight")`. Any project with a non-empty customized pre_release hook — which is common and encouraged — therefore silently loses ALL config-declared external checks from its release gate. Customizing a hook and declaring external checks are orthogonal; one must not disable the other.

## Why this matters

Both failure modes are silent: the config is accepted, nothing warns, the check simply never runs anywhere. A project can believe a guardrail is active for months. Red-green: a test that declares an external check in a fixture config and asserts it (a) appears in check listings, (b) executes during `check --all`, and (c) executes during release preflight even with a customized pre_release hook, would have caught both.

## Workaround in the wild

The affected project duplicated the command into `hooks.pre_release` so the gate is real today; the `external_checks` entry remains declared so it engages when these bugs are fixed. Fixing rlsbl should make that duplication removable.

## Affected files

- `rlsbl/__init__.py` (`_check_context_factory` registration timing)
- `rlsbl/external_checks.py`
- `rlsbl/commands/release/__init__.py` (~line 826 short-circuit)
- New regression tests per the red-green sketch above

## Effort estimate

Small-medium: move/duplicate registration ahead of check filtering, decouple the preflight external-check run from hook customization, plus the three-assertion regression test.
