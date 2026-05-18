---
description: "Configuration reference for rlsbl projects — config.json targets and tagging, workspace.toml for monorepos, and selfdoc.json for docs builds."
---

# Configuration reference

## .rlsbl/config.json

Project-level configuration file created by `rlsbl config init` or `rlsbl scaffold`. This JSON file controls release behavior such as which targets to use (chosen from 14 supported registries) and whether ecosystem tagging is enabled. Settings here override user-level defaults in `~/.rlsbl/config.json` but are themselves overridden by CLI flags passed at release time, forming a 3-layer precedence chain (CLI > project > user).

:-: table-config path=".rlsbl/config.json"

### Key reference

| Key | Type | Description |
| --- | ---- | ----------- |
| targets | array | List of target names to use (overrides auto-detection) |
| tag | bool | Enable/disable ecosystem tagging (default: true) |

Configuration precedence for tagging: CLI flag (`--no-tag`) > project config > user config (`~/.rlsbl/config.json`) > default (true).

:-: ref path="rlsbl.config"

## .rlsbl-monorepo/workspace.toml

Monorepo workspace definition that lists all sub-projects, their relative paths, and optional names. rlsbl walks up from the current directory to find this file, so you can run release commands from within any sub-project. See the [monorepo guide](monorepo.md) for setup instructions, workspace commands, and subtree publishing.

The file uses TOML format with a `[[projects]]` array. Each entry has a `path` key (relative to the monorepo root) and an optional `name` key. If `name` is omitted, the directory basename is used.

## selfdoc.json

When present in the project root, this file enables the `docs` release target and configures documentation builds. It specifies the source directories to scan, the output path for generated pages, the base URL for the published site, and an optional deploy provider such as Cloudflare Pages. See the [selfdoc documentation](https://github.com/smm-h/selfdoc) for the full schema.

:-: table-schema path="selfdoc.json"
