# Rewriting rlsbl in Go: decisions and deviations

Append-only companion to `todo/go-rewrite-plan.md`. Each entry records an implementation decision made along the way, or a deviation between the plan and what the work found, at the moment it is made: what the plan says, what the work showed, and what was built instead. Entries marked "awaiting a ruling" are built as described and stay open until a ruling is appended.

## Folding the orchestrator rulings into the plan

- The standard library's `go/parser` stays in rlsbl, used only for the import sites of `rewrite go-module-path`, the ldflags symbol checks, and the guard tests; rlsbl has no tree-sitter or other parser dependency and analyzes no source for checks.
- Because confidentiality is derived from license periods, `transition declassify` takes a repeatable `--license <releasable>=<SPDX>` and closes the proprietary periods itself, and `transition license` refuses a change that would leave a repository with no proprietary releasable while it is declared private. Awaiting a ruling.
- Release tags inside a squashed proprietary period are moved to the squash commit, and their release archives become `unrecoverable`, since the released tree no longer exists. Awaiting a ruling.
- safegit has no squash rewrite, so the safegit slice adds a squash mode to `safegit scrub`.
- The migration writes a `[python_tools.lint]` table for each pypi member unless its `rlsbl:ruff-lint` option entry is `off`, so `ruff-lint` enforcement continues as strictcode's lint rule.
- selfdoc scans every repository, including ones without a lifecycle-and-license record, and its `repository-visibility` check refuses a private repository with no proprietary releasable. Awaiting a ruling.
- The cgofree tree-sitter modules become `github.com/stricttools/cgofree/generated/<dir>`, tagged `generated/<dir>/vX.Y.Z` starting at `v0.1.0`, and the `/generated/` ignore rule is narrowed so those directories are tracked.
- The minimal-record command for repositories rlsbl does not manage is `rlsbl transition init-minimal-record`.
- Open before any cgofree release (awaiting a ruling): cgofree has no git remote, so the plan makes a public `origin` at `github.com/stricttools/cgofree` a precondition; and cgofree's own `docs/decisions.md` records an owner ruling that tree-sitter ships as one repository per module, which the subdirectory-module ruling contradicts.
- Open before saferm's release (awaiting a ruling): saferm's npm per-platform package names follow `<package>-<os>-<cpu>` without win32, by analogy with rlsbl's approved names.
