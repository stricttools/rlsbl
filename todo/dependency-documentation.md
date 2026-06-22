# Dependency documentation feature

## Problem

Projects have no single place that documents all their dependencies with context. Dependencies live scattered across manifests (pyproject.toml, package.json, go.mod, build.zig.zon) and many important dependencies aren't in manifests at all — CLI tools installed from sibling projects (rlsbl, selfdoc, pgdesign, safegit), language toolchains (uv, pnpm, Zig, Go), infrastructure tools (Caddy, PgBouncer, Prometheus), and vendored libraries. There's no record of *why* a dependency was added, *when*, or what it's used for.

## What we want

A generated markdown file (managed by selfdoc or rlsbl itself) with tables grouping all dependencies by category, showing:

- Dependency name
- Version (or constraint) currently in use
- Which component(s) use it
- Purpose (one-liner)
- When it was added and why (human-authored context)
- Whether it's a runtime dep, dev dep, CLI tool, infra tool, etc.

The version and component data should be scraped automatically from manifests. The human context (purpose, when/why) should come from a metadata file that the developer maintains.

## Proposed approach

Two options for the rlsbl agent to evaluate:

### Option A: Hybrid (rlsbl extracts, project renders)

rlsbl adds a `rlsbl deps` command that outputs structured JSON of everything it can scrape from manifests (it already knows target types: pypi, npm, go, zig). Each project then uses a selfdoc template or custom script to merge that JSON with a project-specific metadata file and render tables.

Pros:
- rlsbl stays focused on data extraction
- Projects own their layout and grouping
- Simpler rlsbl scope

Cons:
- Every project reinvents the rendering
- The metadata file schema isn't standardized
- More moving parts per project

### Option B: Full feature in rlsbl (preferred by the filing project)

rlsbl owns the entire pipeline: manifest scanning, metadata file schema, and markdown generation. Projects maintain a `deps.toml` (or section in an existing config) with human context per dependency. rlsbl generates the markdown as part of `rlsbl docs` or similar, and selfdoc can include it in its gen pipeline.

The metadata file would need to support:
- Dependencies not in any manifest (CLI tools, infra tools, toolchains)
- Grouping/categorization (the project decides what groups exist)
- Per-dependency fields: name, purpose, added (date), reason, notes
- Auto-merge with manifest-scraped data (versions, components, dev/runtime)

Pros:
- One standardized schema and renderer for all rlsbl projects
- Less per-project boilerplate
- rlsbl already has tree-sitter for all relevant languages
- Fits rlsbl's role as project lifecycle tooling

Cons:
- Larger rlsbl scope
- Layout opinions may not fit all projects (but could be configurable via changelog_format-style options)
- External/infra tools are inherently project-specific, so the metadata file does most of the heavy lifting anyway

## Scope of manifest scanning

rlsbl already knows about these target types. Scanning would cover:

| Manifest | What to extract |
|---|---|
| `pyproject.toml` | `[project.dependencies]`, `[project.optional-dependencies]`, build-system |
| `package.json` | `dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies` |
| `go.mod` | `require` block (direct vs indirect) |
| `build.zig.zon` | `.dependencies` |

In monorepo mode, scan all workspace sub-projects and track which component uses what.

## Example output

```markdown
## Runtime Dependencies

| Dependency | Version | Components | Purpose |
|---|---|---|---|
| websockets | latest | server, sdk, agent-sdk, mcp, cubeconnect | Python WebSocket library |
| asyncpg | latest | server, sdk, mcp, cubeconnect | PostgreSQL async driver |
| nhooyr.io/websocket | v1.8.17 | router, sdk-go, benchmark (3) | Go WebSocket library |
| zod | ^4.4.3 | framework | Runtime schema validation |

## Dev Dependencies

| Dependency | Version | Components | Purpose |
|---|---|---|---|
| pytest | >=9.0.3 | executor, server, sdk, agent-sdk, mcp | Python test runner |
| vitest | ^4.1.6 | framework, shell, audio | JS/TS test runner |
| typescript | ^5.8-6.0 | framework, shell, admin, audio | Type checker |

## CLI Tools

| Tool | Version | Purpose | Added |
|---|---|---|---|
| rlsbl | latest | Release orchestration | 2025-01 |
| selfdoc | latest | Doc generation from source | 2025-03 |
| pgdesign | latest | Schema compiler (TOML to SQL) | 2025-02 |

## Infrastructure

| Tool | Purpose | Added |
|---|---|---|
| Caddy | Reverse proxy | 2025-04 |
| PgBouncer | Connection pooling | 2025-05 |
```

## Effort estimate

- Manifest scanning: medium (pyproject.toml and package.json are straightforward; go.mod needs direct/indirect parsing; zig.zon is a Zig struct literal)
- Metadata file schema + parser: small
- Markdown renderer: small-medium (table generation, grouping, column alignment)
- selfdoc integration: small (hook into gen pipeline or standalone command)
- Monorepo support: medium (aggregate across workspace projects, track component membership)

## Notes

- rlsbl already has tree-sitter parsers for Python, JavaScript, TypeScript, and Go — useful for manifest parsing
- The metadata file is where all the human context lives; without it, the output is just a version dump with no "why"
- Consider whether `deps.toml` should be a new file or a section in `.rlsbl/config.json`
