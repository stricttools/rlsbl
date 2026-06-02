# Expand dead-modules check to Go/npm + feature support matrix

## dead-modules expansion

The `dead-modules` quality check currently only works for Python (checks `__init__.py` exports and intra-project imports). Go and npm have different export semantics:

- **Go**: No `__init__.py`. Every exported identifier in a package is public. Dead internal packages (under `internal/`) with zero intra-module importers are detectable. But sibling `cmd/` packages that share nothing aren't dead.
- **npm**: Exports defined via `package.json` `"exports"` field (conditional, nested) or `"main"`. Also `index.js`/`index.ts` convention. Parsing `exports` is complex.

The check should be language-aware from the start. Start with Python (done), add Go internal package detection, then npm exports parsing.

Cross-project dead code (library modules only imported by downstream packages) cannot be detected with intra-project scanning. Would need a global workspace import graph — expensive but correct.

## Feature support matrix

Add a generated table (via selfdoc directives or a standalone script) showing rlsbl feature support per language/ecosystem. Rows = features, columns = ecosystems, cells = support level.

Example:

| Feature | Python/PyPI | Go | npm | Cargo | Dart | Docker |
|---------|------------|-----|-----|-------|------|--------|
| dead-modules | yes | no | no | no | no | no |
| deps-unused | yes | no | yes | no | yes | no |
| deps-undeclared | yes | no | yes | no | yes | no |
| deps-runtime-test-only | yes | no | yes | no | yes | no |
| library-lint | yes | yes | yes | no | no | no |
| import scanning | yes | no | yes | no | yes | no |

This table should be auto-generated from the check/lint system's actual capabilities, not hand-maintained.

## Effort

Medium. Per-language export detection is the hard part. The feature matrix is a selfdoc directive or script.
