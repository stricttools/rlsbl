# Spec target for data-only packages

## Context

The F monorepo has 3 "spec" packages: `sdui_spec`, `llm_spec`, `conformance_spec`. These contain pure data files (YAML, JSON) with no code in any language. They are consumed by code packages (sdui_schema reads sdui_spec, llm_contract reads llm_spec, conformance reads conformance_spec).

The incantino project has a similar pattern: `spec/` contains YAML schema definitions consumed by 4 language-specific clients.

rlsbl doesn't currently have a target type for data-only packages. These packages have no manifest file (no pubspec.yaml, no pyproject.toml, no package.json), no build step, no publish destination, and no lockfile. But they do have versions, changelogs, and dependencies (other packages depend on them).

## What we need

- A `spec` target type that rlsbl recognizes.
- Version tracking: the spec version should be tracked somewhere (a `version.txt` file? a TOML metadata file? the workspace.toml entry?).
- Changelog validation: spec changes should have changelog entries just like code changes.
- Release tagging: spec releases get tags like any other package.
- No publish step: there's no registry to publish to.
- No manifest parsing: dependencies are declared only via `depends_on` in workspace.toml.
- No lockfile syncing.

## Open questions

- Where does the version live for a spec package? There's no manifest file. Options: a `version.txt`, a `spec.toml`, or just the workspace.toml entry.
- Should spec packages have their own changelog format, or use the same CHANGELOG.md as everything else?
- When a spec changes, should rlsbl automatically flag downstream packages as needing a release? (e.g., sdui_spec changes -> sdui_schema should release too)
