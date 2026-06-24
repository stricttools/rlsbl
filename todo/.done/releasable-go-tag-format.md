# Go slash-format tags missing from releasable releases

## Problem

When releasing a releasable that contains Go packages (e.g., auth releasable with auth-gateway and auth-sdk-go), `rlsbl monorepo release run` creates only the releasable-level tag (e.g., `auth@v0.2.0`). It does not create Go-compatible slash-format tags (`auth-gateway/v0.2.0`, `auth-sdk-go/v0.2.0`).

Go module resolution via `go get` requires tags in `<path>/v<major>.<minor>.<patch>` format. The `@` format used by releasable tags is not recognized by the Go toolchain.

## Impact

After the first releasable release of auth, Go consumers will be unable to resolve new versions of auth-gateway or auth-sdk-go via `go get`. The old slash-format tags (auth-gateway/v0.1.0, auth-sdk-go/v0.1.0) will remain the latest resolvable versions.

## Proposed solution

During releasable release, for each member package whose target is `go`, also create a per-package slash-format tag pointing to the same release commit. This is in addition to the releasable-level tag, not a replacement.

## Affected files

- `rlsbl/commands/release/execute.py` — tag creation section
- `rlsbl/targets/go.py` — `monorepo_tag_format()` method
