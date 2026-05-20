# Dependency-import validation

## Context

In a monorepo with 41 packages, declared dependencies and actual imports can drift. A package might declare a dependency it doesn't use (dead dep), or import from a sibling it didn't declare (undeclared dep). Both are problems: dead deps create false coupling in the graph, undeclared deps create invisible coupling that breaks when packages are released independently.

Currently rlsbl reads manifests (pyproject.toml, package.json) to discover dependencies but never checks whether the code actually uses them.

## What we need

For each package in the workspace, rlsbl should be able to answer:

1. **Unused declared deps**: Package A declares B as a dependency, but no source file in A imports from B. This is a dead dependency that should be removed.
2. **Undeclared imports**: A source file in package A imports from package C, but A does not declare C as a dependency. This is an invisible coupling that must be either declared or removed.

This needs to work across languages:

- **Dart**: scan for `import 'package:foo/...'` and `export 'package:foo/...'` statements. The package name after `package:` maps to a sibling workspace package.
- **Python**: scan for `import foo` and `from foo import ...` statements. Map module names to workspace package names (with normalization: hyphens, underscores).
- **Spec/data packages**: these have no imports. But packages that consume them (e.g., sdui_schema reads from sdui_spec) need a way to declare this dependency. The `depends_on` field in workspace.toml handles this.

## When it should run

- `monorepo lint` should include this check.
- CI should fail on undeclared imports.
- CI should warn on unused declared deps (warning, not error, because some deps are used at runtime via DI without explicit imports).

## Edge cases

- Dev dependencies: test files may import from packages that are only dev_dependencies. The scanner needs to distinguish `lib/` imports from `test/` imports.
- Transitive imports: if A depends on B and B depends on C, A can technically import C's types through B's re-exports. But A should still declare C if it imports directly from C.
- Code generation: generated files (e.g., Drift, Freezed, json_serializable) may produce imports that don't appear in hand-written source. The scanner should read all `.dart` files including generated ones.
- DI/runtime deps: some dependencies are wired at runtime (e.g., `app/` depends on `auth/` for DI but may never directly import from it in lib/). These are legitimate declared-but-not-imported cases. The `depends_on` field in workspace.toml could be used to whitelist these.
