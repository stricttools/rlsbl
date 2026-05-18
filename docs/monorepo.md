---
description: "Guide to rlsbl monorepo workspaces — initializing workspaces, adding projects, workspace.toml format, and subtree publishing."
---

# Monorepo guide

rlsbl supports monorepo workflows via the `rlsbl monorepo` command family, which provides 10 subcommands for managing workspaces with multiple independently-versioned projects. A single workspace can contain any mix of the 14 supported release targets, all coordinated through one `.rlsbl-monorepo/workspace.toml` file at the repository root.

## Getting started

```bash
# Initialize a monorepo workspace
rlsbl monorepo init

# Add a project
rlsbl monorepo add --name mylib --path packages/mylib

# List projects
rlsbl monorepo list

# Show workspace status
rlsbl monorepo status

# Release a specific project from its directory
cd packages/mylib
rlsbl release patch
```

## workspace.toml format

The workspace file lives at `.rlsbl-monorepo/workspace.toml` and declares every sub-project in the repository. Each project is a TOML table in the `[[projects]]` array with a required `path` field and an optional `name` field that defaults to the directory basename. rlsbl validates this structure on load and raises clear errors for missing or malformed entries.

```toml
[[projects]]
path = "packages/mylib"
name = "mylib"

[[projects]]
path = "packages/cli"
name = "cli"
```

Fields per project:

| Field | Required | Description |
| ----- | -------- | ----------- |
| path | yes | Relative path from repo root to the project directory |
| name | no | Project name (defaults to basename of path) |

## Workspace module

The workspace module handles discovery, loading, saving, and resolution of monorepo workspaces. It walks the directory tree upward to locate the nearest `workspace.toml`, parses the TOML structure into validated project entries, and writes changes back atomically using tomlkit to preserve formatting and comments.

:-: ref path="rlsbl.workspace"

## Watch paths

Each project in a monorepo tracks changes independently based on its declared path in the workspace configuration. When you run a release command from within a project directory, rlsbl walks up the directory tree to find the workspace root, then matches your current directory against the registered project paths to determine which project you are releasing.

## Subtree publishing

Monorepo projects can be published to separate repositories using git subtree, which allows each sub-project to maintain its own standalone repository for consumers who do not want the full monorepo. The `rlsbl monorepo sync` command handles pushing subtrees to their configured remotes, keeping the split repositories in sync with the monorepo source of truth after each release.
