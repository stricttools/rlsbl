+++
description = "Documentation index for rlsbl, a release orchestration CLI written in Go that bumps versions, validates a JSONL changelog, tags only the commit CI verified, publishes to npm, PyPI, and the Go module proxy, and scaffolds CI."
+++

# rlsbl

rlsbl releases repositories: it bumps versions, validates a structured JSONL changelog, pushes the release commit untagged and tags it only once the repository's own CI passed on it, creates the GitHub Release, and publishes to npm, PyPI, and the Go module proxy. It scaffolds the CI and publish workflows that go with it, and handles single projects and workspaces of independently versioned releasables alike. Every record it keeps lives under `.strictmetadata/`.

## Getting started

Install rlsbl with `go install github.com/stricttools/rlsbl/cmd/rlsbl@latest`, `npm i -g rlsbl`, or `uv tool install rlsbl` (the npm and PyPI packages carry the prebuilt binary). Then:

- declare the repository in `.strictmetadata/releasables/releasables.toml` ([declarations](declarations.md)), or `rlsbl monorepo init` for a workspace;
- `rlsbl scaffold` to render its workflows and hooks;
- `rlsbl changelog add` for each change, `rlsbl release init`, then `rlsbl release run --watch --approve-consequential`.

## Guides

- [Release workflow](release-workflow.md): the release file, validation, the release checkout, the step table, the CI verdict, resume, the version fates, and the commands on past releases
- [Changelog](changelog.md): JSONL entries, validation and coverage, fork coverage, and the generated `CHANGELOG.md`
- [Check system](checks.md): every check by tag, options, the strictcode check, and purity
- [Scaffold](scaffold.md): the rendered workflows and hooks, scratch directories, private paths, and the three-way merge
- [Development workflow](dev-workflow.md): local installs, editable overlays, watching CI, and the pre-push hook
- [Utility commands](utilities.md): status, unreleased, targets, discover, names, secrets, and rewrites

## Reference

- [Declarations](declarations.md): `releasables.toml` and `test-runner.toml`, key by key
- [On-disk layout](on-disk-layout.md): every `.strictmetadata/` directory rlsbl owns, residue of the old layout, and the record migration
- [Lifecycle and license](lifecycle-and-license.md): the record of each releasable's lifecycle, license, and identities, confidential repositories, and the `transition` commands
- [Release targets](targets.md): go, npm, and pypi, version files, companion tags, and the support matrix
- [Pipelines](pipelines.md): publishing from CI or locally, Go binaries on npm and PyPI, and private-repository rules
- [Workspaces](monorepo.md): members, releasables, the dependency graph, the CI router, and batch releases
- [Repository conversions](conversions.md): extract, absorb, renames, and the transition record
- [CI customization](ci-customization.md): workflow files scaffold never touches
- [CLI reference](cli-index.md): every command and flag (generated)
- [API reference](gen-index.md): the Go packages (generated)
