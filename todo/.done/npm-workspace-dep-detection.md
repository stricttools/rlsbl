# npm workspace protocol dependency detection gap

## Context

rlsbl's `deps-unused` check scans source files for `import`/`require` statements to determine whether declared dependencies are actually used. This works well for standard npm packages, but fails for npm/pnpm workspace projects that use the `workspace:*` protocol.

## Problem

In monorepo workspaces, inter-project dependencies are declared in `package.json` using the workspace protocol:

```json
{
  "dependencies": {
    "framework": "workspace:*"
  }
}
```

TypeScript source files then import from these packages by name (e.g., `import { thing } from "framework"`), and npm/pnpm resolves them via the workspace protocol at install time. However, rlsbl's source scanner only looks at `.ts`/`.js` files for `import`/`require` patterns. When the module name resolution happens through npm's workspace protocol rather than a direct file path, the scanner may not find a matching import, causing a false positive: the dependency IS used but rlsbl reports it as unused.

Real example: gamehome's `gg-shell` project declares `depends_on = ["framework"]` in `workspace.toml`. The actual runtime dependency is in `shell/package.json` as `"framework": "workspace:*"`. The TypeScript source imports from `"framework"`, which npm resolves via workspace linking. rlsbl's scanner misses this because the resolution is indirect.

## Proposed solution

When scanning npm/pnpm projects for dependency usage, also inspect the project's `package.json` for `workspace:*` (and `workspace:~`, `workspace:^`) entries in `dependencies` and `devDependencies`. If a workspace project appears as a `workspace:` dependency in `package.json`, count it as "used" regardless of whether the source file scanner finds a matching import. The `workspace:` protocol is an explicit declaration of usage — the package manager enforces the link at install time.

This should be a supplementary check layered on top of the existing source scanner, not a replacement. The source scanner remains the primary mechanism; workspace protocol entries just provide an additional signal that prevents false positives for workspace-linked packages.

## Affected code

- The `deps-unused` check implementation (wherever source file scanning happens for npm targets)
- `package.json` parsing logic — needs to recognize `workspace:*`/`workspace:~`/`workspace:^` version specifiers
- Potentially the monorepo dependency resolution code if it already parses `package.json`

## Effort

Small. The fix is a targeted addition to the existing dependency scanner: read `package.json`, check for `workspace:` prefixed versions, and merge those into the "used" set before reporting unused deps.
