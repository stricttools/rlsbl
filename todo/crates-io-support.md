# crates.io support for rlsbl

Filed: 2026-07-04

## Problem

rlsbl supports npm and PyPI as namespaced targets (check-name, claim-name, thin shim wrappers for cross-language distribution). Rust's crates.io is a third major central registry with a flat global namespace, but rlsbl has no support for it.

Go tools in the ecosystem (safegit, saferm, migrable, pgdesign, howmuchleft, go-toml-edit, wakethemup) currently ship thin shim wrappers to npm and PyPI so users can `npm install -g <tool>` or `pip install <tool>` and get a working CLI that downloads the Go binary. There is no equivalent for crates.io, meaning Rust users have no install path other than downloading the binary directly from GitHub Releases.

## Context: how crates.io works

- Central registry, flat global namespace (like npm/PyPI).
- `cargo install <crate>` compiles from source -- no post-install hook, no script-as-entry-point like npm/PyPI.
- `cargo-binstall` is the ecosystem-standard tool for installing prebuilt binaries: users run `cargo-binstall <crate>` instead of `cargo install`, and it downloads a prebuilt binary from GitHub Releases (with automatic source-build fallback). Configured via `[package.metadata.binstall]` in Cargo.toml.
- Names are permanent: `cargo yank` hides a version from new resolution but does NOT free the name. No unpublish, no delete, ever. Strictest permanence of any registry.
- No explicit policy ban on download-a-binary shims via build.rs, but it's not the endorsed pattern. The policy RFC explicitly left this question unresolved.
- Requires a crates.io account (GitHub login) and API token for publishing.

## Scope

### 1. check-name for crates.io

`rlsbl check-name <name> --target crates` should check:
- Direct availability on crates.io (their API: `GET https://crates.io/api/v1/crates/<name>`)
- Moniker/similarity checks (crates.io normalizes hyphens and underscores as equivalent: `dir-stat` == `dir_stat` == `dirstat`)
- Report whether the name is claimable

### 2. claim-name for crates.io

`rlsbl claim-name <name> --target crates` should publish a minimal placeholder crate to lock the name under the user's crates.io account. Since crate names are permanent (no unpublish), this is an even more consequential operation than npm/PyPI claim-name.

Needs:
- Scaffold a minimal Cargo.toml + src/lib.rs (or src/main.rs with a placeholder message)
- Run `cargo publish` (requires crates.io API token)
- Authentication: crates.io token management (stored where? env var? `.cargo/credentials.toml`?)

### 3. Thin shim wrapper scaffolding for crates.io

Two viable approaches to evaluate:

**Approach A: build.rs shim (works with `cargo install`)**
- Scaffold a Rust crate where build.rs downloads the correct prebuilt binary from GitHub Releases at compile time
- The Rust binary is just a thin exec wrapper
- Pro: works with standard `cargo install`
- Con: requires a Rust toolchain to "install" a Go/Python binary (weird UX), compile step is pointless overhead

**Approach B: cargo-binstall metadata (works with `cargo-binstall`)**
- Scaffold a minimal crate with `[package.metadata.binstall]` in Cargo.toml pointing to GitHub Releases (goreleaser archive URLs)
- Users run `cargo-binstall <name>` to get the prebuilt binary directly
- Automatic fallback to source build via `cargo install` if binstall unavailable
- Pro: clean, no compilation overhead, ecosystem-standard for prebuilt distribution
- Con: requires users to have `cargo-binstall` installed (extra step vs npm/PyPI)

**Approach C: both**
- Scaffold the crate with binstall metadata AND a functional build.rs shim, so both `cargo install` and `cargo-binstall` work
- Most coverage, most scaffolding complexity

### 4. Scaffold integration

`rlsbl scaffold` for projects with a `crates` target should generate:
- `crates/` wrapper directory (parallel to existing `npm/` and `pypi/` wrapper dirs)
- Cargo.toml with name, version, description, license, repository, and binstall metadata
- Minimal Rust source (shim or placeholder depending on approach chosen)
- CI workflow for `cargo publish` (crates.io supports OIDC trusted publishing? -- needs investigation; if not, API token via GitHub secret)

### 5. Release pipeline integration

During `rlsbl release run`, for projects with a `crates` target:
- Bump version in the wrapper's Cargo.toml
- Run `cargo publish` from the wrapper directory (after the main release artifacts are pushed, so the GitHub Release with binaries exists before the shim tries to reference them)
- If crates.io supports publish gating on CI (like PyPI OIDC trusted publishing), use it

## Open questions

- Does crates.io support OIDC trusted publishing (like PyPI), or does it require a static API token?
- Should approach A (build.rs), B (binstall), or C (both) be the default for Go/Python tool shims?
- Where should the crates.io API token live for claim-name and release? `~/.cargo/credentials.toml` is cargo's standard, but rlsbl may want its own management (like npm token in `~/.npmrc`).
- Should `rlsbl check-name --all` (if it exists or is planned) include crates.io alongside npm/PyPI?
- Naming normalization: crates.io treats hyphens and underscores as equivalent. rlsbl's check-name moniker logic needs to include this rule for the crates target.
