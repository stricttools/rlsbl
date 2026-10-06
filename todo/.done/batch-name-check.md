# Batch Name Availability Checking

Status: Proposed
Priority: Medium

## Context

`rlsbl check <name>` currently accepts a single name and defaults to checking both npm and PyPI when no `--target` is specified. Two problems:

1. Checking multiple names requires running the command N times manually.
2. The implicit default of checking npm+pypi risks IP bans when scaled up and violates the principle of no implicit defaults for registry selection.

## Features

### 1. Require `--target` (breaking change)

Remove the default `["npm", "pypi"]` fallback. `--target` becomes required for all `rlsbl check` invocations. Pre-stable, so clean break.

### 2. Multi-name CLI

```
rlsbl check foo bar baz --target pypi
```

Accept multiple positional names. Single name prints the current verbose format (with variants and GitHub info). Multiple names print a compact table:

```
Name    Status
foo     available
bar     TAKEN
baz     available
```

No variant expansion in table mode.

Requires internal refactoring: extract `_check_single_name(name, registry)` returning a structured result, and separate formatters for verbose vs. table output.

### 3. Rate limiting

Two mechanisms to prevent IP bans:

- `--delay` flag (default 200ms) applied between names (not between variant checks within a single name)
- HTTP 429 detection with exponential backoff on PyPI/Go/GitHub urllib calls. Retry up to 3 times, respecting `Retry-After` header.

npm uses subprocess (`npm view`), so 429 handling is npm's responsibility -- just apply the inter-name delay.

### 4. `rlsbl monorepo check-names`

New monorepo subcommand. Reads project names from `workspace.toml`, passes them to the multi-name check.

- `--target` required
- `--prefix <string>` prepends to each name before checking
- `--suffix <string>` appends to each name

Output:

```
Project    Checked Name    Status
core       portal-core        available
api        portal-api         TAKEN
utils      portal-utils       available
```

## Affected Files

- `rlsbl/commands/check.py` -- refactor internals, accept multiple names, add rate limiting
- `rlsbl/commands/monorepo.py` -- add `check-names` subcommand
- `rlsbl/__init__.py` -- remove default registries for check command, update help text

## Effort Estimate

Small-medium. The checking functions already exist. Main work is CLI refactoring, rate limiting, and the monorepo integration.
