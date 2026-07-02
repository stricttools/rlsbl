---
description: "Documentation index for rlsbl, a release orchestration CLI handling version bumps, JSONL changelogs, CI scaffolding, and GitHub Releases for 18 ecosystems."
---

# rlsbl

rlsbl is a release orchestration CLI that handles version bumping, structured JSONL changelogs, CI scaffolding, tagging, and GitHub Releases across 18 ecosystems. It supports single-target projects, multi-target projects, and monorepo workspaces with independent versioning.

## Getting started

Install rlsbl via `uv tool install rlsbl` (Python) or `npx rlsbl` (npm wrapper). Initialize a project with `rlsbl scaffold` to generate CI workflows, git hooks, and changelog infrastructure. See the [README](https://github.com/smm-h/rlsbl#readme) for full installation instructions and quick start examples. Key commands:

- `rlsbl scaffold` -- set up a new project
- `rlsbl release run --watch --yes` -- perform a release
- `rlsbl status` -- check current state

## Guides

- [Release workflow](release-workflow.md) -- end-to-end release process, bump types, release file format
- [Changelog system](changelog.md) -- JSONL entries, validation, coverage enforcement
- [Scaffold and templates](scaffold.md) -- CI/CD generation, three-way merge, hooks
- [Check system](checks.md) -- 57 diagnostic checks across 6 primary tags
- [Deployment](deploy.md) -- deploy targets, post-release hooks, Cloudflare Pages
- [Development workflow](dev-workflow.md) -- editable installs, pre-push hook, local testing
- [Utility commands](utilities.md) -- status, discover, migrate, record-gif, and other helpers

## Architecture

- [Import scanning](import-scanning.md) -- tree-sitter-based dependency detection
- [Dependency validation](dep-validation.md) -- cross-project dependency checks
- [Pipeline architecture](pipelines.md) -- publish pipeline types, asset uploads, configuration
- [Layer enforcement](layers.md) -- architectural layer rules for monorepos
- [Native mobile targets](native-targets.md) -- Android and iOS version bumping

## Reference

- [Configuration](configuration.md) -- `.rlsbl/config.json` format and all keys
- [Release targets](targets.md) -- the 18 supported ecosystems
- [Monorepo](monorepo.md) -- workspace management, subtree publishing, batch releases
- [CI customization](ci-customization.md) -- custom workflow files that survive scaffold
- [CLI reference](cli-index.md) -- all commands and options (auto-generated)
- [API reference](gen-index.md) -- module documentation (auto-generated)
