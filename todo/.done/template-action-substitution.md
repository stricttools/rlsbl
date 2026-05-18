# Template-time substitution of GitHub Actions versions

## Context

Action versions used by scaffolded workflows are centralized in `rlsbl/data/action_versions.toml`. The table is the single source of truth, but the actual template files (`.tpl`) under `rlsbl/templates/**/` still embed literal `name@vX` strings (e.g. `actions/checkout@v6`). A consistency test (`tests/test_action_versions.py::test_templates_match_table`) walks every template and verifies the embedded versions match the table.

This works -- a drift between table and templates is caught in CI -- but it requires editing the version in two places (table + every template that uses the action) for every bump. The test is a guardrail against forgetting, not a structural fix.

## Problem

- Bumping a single action version means touching the TOML plus N template files.
- Adding a new action to a template means remembering to also add it to the table (or the consistency test fails after the fact).
- Templates lie about being "the source" -- readers see `actions/checkout@v6` in `ci.yml.tpl` and don't realize the canonical version lives elsewhere.
- The test is reactive: it catches drift after a bad edit, instead of making drift impossible.

The repeated guardrails-against-the-same-bug-class pattern (see `CLAUDE.md` "step back and ask what structural change makes that class impossible") suggests the right move is to remove the duplication entirely.

## Proposed solution

Phase 2 option (b): substitute action references at scaffold time.

1. Define a placeholder syntax in `.tpl` files, e.g. `{{action.checkout}}` or `{{ action "checkout" }}`. Pick one that is unambiguous in YAML and unlikely to collide with existing template directives.
2. Extend the template renderer (likely `rlsbl/scaffold.py` or wherever `.tpl` interpolation happens) to resolve placeholders against `action_versions.toml` at scaffold time. Unknown action keys raise `UnknownActionError` and abort the scaffold, matching the existing no-implicit-defaults convention.
3. Rewrite every `.tpl` under `rlsbl/templates/` to use placeholders instead of literal `name@version` strings.
4. Replace `test_templates_match_table` with a test that verifies every placeholder used in templates resolves against the table (so unused entries are also flagged, optionally).
5. Update `rlsbl/data/action_versions.toml` documentation to describe the placeholder syntax and link templates to the table.

## Alternatives considered

- **(a) Keep the consistency test, automate bulk edits.** A script that bumps the TOML and rewrites every template. Reduces toil but keeps the duplication. Templates still lie about being the source.
- **(c) Generate templates from a higher-level description.** Overkill -- templates are stable, the only churn is action versions.

Option (b) is the most correct solution: it makes the table the only place a version lives.

## Affected files

- `rlsbl/scaffold.py` (or the equivalent module that renders `.tpl` files) -- placeholder resolver
- `rlsbl/action_versions.py` -- export a `resolve_placeholder(key)` helper if needed
- Every `.tpl` under `rlsbl/templates/**/` containing a pinned action reference (checkout, setup-node, setup-python, setup-go, gh-action-pypi-publish, etc.)
- `tests/test_action_versions.py` -- replace `test_templates_match_table` with placeholder-resolution checks
- `docs/rlsbl-action_versions.md` (auto-generated) -- updates automatically from module docstring once the new behavior lands

## Risks

- Placeholder syntax must not collide with existing template variable handling. Audit `rlsbl/scaffold.py` for the current `.tpl` rendering flow before picking syntax.
- One-shot migration: every template must move to placeholders at once, otherwise the consistency test gets confused about which files it should check. Stage the work in a single PR.
- Three-way merge in `scaffold --update`: ensure rendered (post-substitution) content is what gets merged, so user customizations on the rendered file continue to work.

## Effort

Medium. Renderer change is small; mechanical template rewrite is the bulk of the work; test rewrite is straightforward. A single afternoon if the renderer is easy to locate, longer if the template pipeline turns out to be complicated.

## Related

- `rlsbl/data/action_versions.toml` -- the table
- `tests/test_action_versions.py::test_templates_match_table` -- the current consistency guardrail this todo would replace
- `CLAUDE.md` "No implicit defaults" -- aligns with the convention that missing action entries should raise, not silently substitute
