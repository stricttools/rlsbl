The `changelog validate` command uses `git describe --tags --abbrev=0 --match 'v*'` to find the last release tag. In monorepos, this matches any tag starting with `v` (e.g., the conformance `v0.1.1` tag) instead of the project-scoped tag (e.g., `go/v0.2.0`).

This causes `in_range` validation to use the wrong base tag, producing hundreds of false "hash not in unreleased range" errors. The `status` command correctly uses the project-scoped tag (`go/v0.2.0`), so only `validate` is affected.

The fix: in monorepo mode, `validate` should use the project-scoped tag pattern (e.g., `go/v*` for the go sub-project) instead of bare `v*`.

Discovered during strictcli monorepo releases (go-strictcli v0.2.0 and v0.3.0).
