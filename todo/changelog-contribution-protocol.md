# Generic changelog contribution protocol for third-party tools

Filed 2026-08-03. Design direction was chosen deliberately: rlsbl defines a contribution protocol; external tools implement it. rlsbl never learns what the contributors' domains are.

## Context

External tools in the ecosystem produce release-relevant artifacts (first concrete case: a CLI-surface delta report — "flag X removed, command Y added" — generated at release time from schema diffing). Today there is no way to get such content into CHANGELOG.md or GitHub Release notes without hardcoding tool-specific knowledge into rlsbl. The precedent for tool-agnostic integration already exists: `external_checks` (config-declared subprocess checks, hard-fail semantics, zero domain knowledge in rlsbl). This is its changelog-shaped sibling.

## Constraints (verified in code)

- CHANGELOG.md is a pure function of the JSONL files + archived release TOML + config (`changelog/generate.py:391`, re-derived post-archival at `execute.py:1918-1931`, and by standalone `rlsbl changelog generate`). Injected content that is not persisted in one of those sources evaporates on the next regeneration and dirties the tree.
- Both persistence surfaces are strictspec-gated with unknown-key rejection (JSONL via `.strictspec/changelog-entry-commit-mode.schema.toml`; release file via `.strictspec/release-file.schema.toml` — and `release undo` restores the archived TOML to `unreleased.toml`, which is then re-validated, `release_file.py:398-452`).
- The GitHub Release notes body is frozen at `commands/release/__init__.py:778` — BEFORE the schema dump at `:844`. Any contribution that must appear in notes requires reordering the flow (move regeneration+contributions above changelog generation, or recompute the entry after preflight). `rlsbl release edit` re-syncs notes from CHANGELOG.md, so making CHANGELOG the single source and letting notes inherit is the coherent shape.

## Design

- **Config**: `changelog_contributors = [{name, command, cwd?}]` in `.rlsbl/config.json` (mirroring `external_checks` field shape; command-binary existence validated at registration).
- **Execution point**: during release, after tool-specific regeneration (schema dump, hooks) and BEFORE changelog generation/entry freeze — this is the flow reorder, done once, generically.
- **Contract**: contributor prints structured JSON on stdout — `{name, title, markdown, machine?}`. Non-zero exit is a hard release error (no warn-and-continue).
- **Persistence**: one new generic field in the archived release TOML (`[[contributions]]`: name, title, markdown) — a single `.strictspec/release-file.schema.toml` edit + validator regeneration serves every future contributor. NOT the JSONL layer: entries there are commit-coverage-shaped; contributions are release-shaped, exactly like the existing `description`/`context` fields.
- **Rendering**: `generate_version_section` (`changelog/generate.py:113`) emits each contribution as a titled subsection under the version heading; GitHub notes inherit via the existing derivation. Releasable mode: contributions attach to the releasable's changelog; multi-member attribution via the existing `packages`-prefix precedent (`generate.py:164-166`).
- **Interaction with undo/edit**: `unfinalize_release_file` must carry the contributions field through the round-trip; `release edit` re-sync includes contributed sections automatically (they live in the archived TOML).

## Options considered and rejected

- Tool-specific changelog sections hardcoded in rlsbl: couples rlsbl to every domain; rejected.
- Machine-readable HTML markers in GH notes only (CI-SHA marker precedent, `execute.py:2189`): notes become the store of record and CHANGELOG regeneration can't reproduce them; rejected as primary (fine as an additive marker later).
- Contributions in JSONL entries: shape mismatch (commit-scoped vs release-scoped); rejected.

## Affected files

`.strictspec/release-file.schema.toml` + regenerated validator, `rlsbl/release_file.py` (read/bind/unfinalize), `rlsbl/changelog/generate.py` (render), `rlsbl/commands/release/__init__.py` (flow reorder + contributor execution), `rlsbl/external_checks.py` (shared subprocess plumbing candidates), docs (`docs/release-workflow.md` step order is a documented contract and changes).

## Dependencies

Lands together with the release-flow reorder that the CLI-surface gate's changelog rendering needs (`todo/cli-surface-release-gate.md` gates; this renders). First implementor: the CLI-surface delta section.

## Effort

M. The strictspec schema edit + validator regen + flow reorder dominate; rendering is small.
