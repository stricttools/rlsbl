# CLAUDE.md missing monorepo release workflow and commands

## Problem

The RLSBL protocol in `~/Projects/CLAUDE.md` only documents the single-project release flow (`rlsbl release init`). It does not mention:

1. `rlsbl monorepo release-init` — the monorepo equivalent that scaffolds `.rlsbl-monorepo/releases/unreleased.toml`
2. The monorepo release file path (`.rlsbl-monorepo/releases/unreleased.toml` vs `.rlsbl/releases/unreleased.toml`)
3. The `[releasables.<name>]` table structure in the batch release file

AI agents following the RLSBL protocol will run `rlsbl release init` from a monorepo root and get an error ("CWD is inside monorepo but not inside any registered project"), then have to guess the correct monorepo command.

## What to update

### RLSBL protocol step 4

Current: "Run `rlsbl release init` to scaffold `.rlsbl/releases/unreleased.toml`"

Should also mention: In monorepo mode with releasables, use `rlsbl monorepo release-init` instead. This scaffolds `.rlsbl-monorepo/releases/unreleased.toml` with `[releasables.<name>]` sections.

### Reference section

Missing monorepo commands:
- `rlsbl monorepo release-init` — scaffold batch release file for all releasables
- `rlsbl monorepo release-order` — show topological release order
- `rlsbl monorepo outdated` — detect version drift
- `rlsbl monorepo mirror` / `extract` / `absorb` — repo operations
- `rlsbl monorepo extract-releasable` / `migrate-releasable` — releasable management

The reference lists `rlsbl monorepo release` and `rlsbl monorepo graph` but misses 13+ other monorepo subcommands.
