# Expand dead-modules check to Go/npm

## dead-modules expansion

The `dead-modules` quality check currently only works for Python (checks `__init__.py` exports and intra-project imports). Go and npm have different export semantics:

- **Go**: No `__init__.py`. Every exported identifier in a package is public. Dead internal packages (under `internal/`) with zero intra-module importers are detectable. But sibling `cmd/` packages that share nothing aren't dead.
- **npm**: Exports defined via `package.json` `"exports"` field (conditional, nested) or `"main"`. Also `index.js`/`index.ts` convention. Parsing `exports` is complex.

The check should be language-aware from the start. Start with Python (done), add Go internal package detection, then npm exports parsing.

Cross-project dead code (library modules only imported by downstream packages) cannot be detected with intra-project scanning. Would need a global workspace import graph — expensive but correct.

## Effort

Medium. Per-language export detection is the hard part.
