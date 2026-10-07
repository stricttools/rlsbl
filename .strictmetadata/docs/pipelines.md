+++
description = "Publish pipelines: the go, npm, and pypi pipeline types and their artifacts, publishing from CI or from this machine, Go binaries shipped through npm platform packages and PyPI binary wheels, Homebrew taps, and the publishing rules of private repositories."
+++

# Pipelines

A **target** decides which files a release writes the version into; a **pipeline** decides where the release is published. Targets come from the member's manifests or its `targets` declaration ([release targets](targets.md)); pipelines are always declared, as `[[members.pipelines]]` tables in `.strictmetadata/releasables/releasables.toml`. A member with no pipelines still releases (version, tag, GitHub Release) and publishes to no registry, and a releasable declaring `publish_mode = "none"` publishes nothing at all and gets no publish workflow.

```toml
[[members.pipelines]]
name = "npm"            # the pipeline's name: letters, digits, '-' and '_'
type = "npm"            # go, npm, or pypi
target = "npm"          # a target the member has; the pipeline's type must be that target
local = false           # true publishes from this machine during the release; false from CI
artifact = "package"    # what it publishes (below)
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique within the member. |
| `type` | yes | `go`, `npm`, or `pypi`, the same as the target it publishes. |
| `target` | yes | A target the member has. |
| `local` | yes | `false`: the publish workflow publishes from CI after the GitHub Release is published. `true`: the release publishes from this machine at its `pipelines-published` step. |
| `artifact` | yes | go: `binary` or `library`. npm and pypi: `package`, or `go-binary` to ship a go binary pipeline's binaries. |
| `install_paths` | go, when `local = true` | The main packages a local go publish installs. Allowed on go pipelines only. |
| `homebrew_tap` | no | The Homebrew tap repository a go `binary` pipeline publishes a formula to. |
| `binary_pipeline` | npm and pypi `go-binary` | The member's go `binary` pipeline whose binaries it ships. Refused on any other pipeline. |

## Pipeline types

:-: table-pipelines

The `ci-publish-secrets` check asks GitHub whether each secret a CI pipeline authenticates with exists on the repository, and `npm-token-synced` whether the `NPM_TOKEN` secret holds the token npm accepts on this machine; `rlsbl secrets sync-npm-token` copies it there.

## Publishing from CI

A releasable with `publish_mode = "ci"` gets `.github/workflows/publish.yml` from `rlsbl scaffold` (in a workspace, the publish router `rlsbl monorepo sync --auto-commit` generates). It runs on `release: published` and on `workflow_dispatch`, starts with the [wait-for-ci job](release-workflow.md#the-publish-workflow), and has one job per pipeline publishing from CI:

- **go `library`**: asks the Go module proxy for the new version, so the module is listed and `pkg.go.dev` indexes it.
- **go `binary`**: builds the binaries with goreleaser (`.goreleaser.yml`, which scaffold renders), scans them for secrets with gitleaks, and attaches the release archives to the GitHub Release; with `homebrew_tap`, goreleaser publishes the formula too.
- **npm `package`**: packs the package, scans it for secrets with gitleaks, skips a version npm already lists, and runs `npm publish`, with build attestations (`--provenance`) when the repository may record its identity publicly.
- **pypi `package`**: builds the sdist and wheel, scans them for secrets with gitleaks, and publishes through trusted publishing, with attestations unless the repository may not record its identity publicly. The CI workflow checks the built sdist and wheel for [private paths](scaffold.md#private-paths) on every push.
- **npm `go-binary`** and **pypi `go-binary`**: below.

## Go binaries on npm and PyPI

A go `binary` pipeline's binaries can also ship through npm and PyPI, the way esbuild ships its binary, with no download at install time and no install script. One platform table serves both registries:

| Platform | npm `os` and `cpu` | Go `GOOS`/`GOARCH` | PyPI wheel platform tag |
| --- | --- | --- | --- |
| `linux-x64` | `linux`, `x64` | `linux`/`amd64` | `manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64` |
| `linux-arm64` | `linux`, `arm64` | `linux`/`arm64` | `manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64` |
| `darwin-x64` | `darwin`, `x64` | `darwin`/`amd64` | `macosx_10_12_x86_64` |
| `darwin-arm64` | `darwin`, `arm64` | `darwin`/`arm64` | `macosx_11_0_arm64` |

There is no Windows platform.

- An **npm `go-binary`** pipeline publishes one platform package per platform, named `<main package>-<platform>` (for `rlsbl`: `rlsbl-linux-x64`, `rlsbl-linux-arm64`, `rlsbl-darwin-x64`, `rlsbl-darwin-arm64`), each holding that platform's binary from the goreleaser archive with its `os` and `cpu` fields, and then the main package, whose `optionalDependencies` pin every platform package to the version and whose `bin/index.js` launcher (rendered by scaffold) runs the binary of the platform package npm installed. The main package's `package.json` is the project's own; scaffold checks it names the launcher and declares no install script. Each platform package takes its license from the [lifecycle-and-license record](lifecycle-and-license.md), and carries `repository` only where the repository may be named publicly.
- A **pypi `go-binary`** pipeline assembles one wheel per platform, `<distribution>-<version>-py3-none-<platform tag>.whl`, with the binary under `<distribution>-<version>.data/scripts/` and its `METADATA`, `WHEEL`, and `RECORD`, and publishes them through trusted publishing.

A platform package name is a new registry name, and every registry name, once held, is held for good ([the rules](lifecycle-and-license.md#the-rules)).

## Publishing from this machine

A pipeline with `local = true` publishes during the release, after the tag is pushed and the GitHub Release exists:

- **go**: notifies the Go module proxy of `<module>@v<version>` (`go list -json -m` with `GOPROXY=proxy.golang.org`) and runs `go install` for each of `install_paths`, which are validated as main packages first.
- **npm**: `npm publish --access public` from the target's directory, with `NPM_TOKEN` in the environment (the project's `.npmrc` reads it, so the token never becomes an argument).
- **pypi**: `uv build` and `uv publish`, with `PYPI_TOKEN` as uv's publish token.

npm and PyPI are first asked, through the package's listing of every version, whether the version is already there; a listed version is skipped, so a resumed release does not publish twice. A missing token, or a missing `go`, `npm`, or `uv`, is an error naming it. A `go-binary` pipeline cannot publish locally: its platform packages come from release archives only CI builds.

## Private repositories and proprietary releasables

What a pipeline may publish depends on the [lifecycle-and-license record](lifecycle-and-license.md) and on GitHub's visibility of the repository:

- A **proprietary** releasable publishes nothing anywhere public: every pipeline it declares is refused by release validation.
- A **confidential or private** repository publishes nothing that records the repository's identity publicly: no npm `--provenance`, no PyPI attestations, no Go module proxy request (so no go `library` pipeline and no local go pipeline), no Homebrew tap, and no manifest field naming the repository (`package.json` `repository`, `homepage`, `bugs`; `pyproject.toml` `[project.urls]`). A Go client of such a repository ships as binaries only, through npm platform packages and PyPI binary wheels. Scaffold renders the publish workflows of such a repository without attestations, proxy requests, or repository URLs; the `private-repo-publishing` check and release validation refuse what remains, each finding naming its fix.

Before anything publishes, the release packs every artifact a publishing releasable ships (the npm tarball, the wheel, a Go library's module zip, and the files a Go binary is built from) and refuses any file that comes from outside the releasable's own members, naming each file and the artifact carrying it. In a public repository, every text file in those artifacts, the GitHub Release body, and the published changelog section are scanned for the names in the machine-local confidential-name index.
