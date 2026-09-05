# Complete the config schema and police .rlsbl/config.json

## Context

docs/configuration.md's "policed configuration surfaces" section names
workspace.toml (member tables, releasable tables, top level) and the
standalone releasable file as policed — unknown keys refused at load —
and explicitly names .rlsbl/config.json as UNPOLICED pending this item.
The config-schema check today bans exactly three known-bad shapes and
never rejects an unknown key: a typo'd key in config.json still means
nothing at all and nothing says so.

## Problem

The one remaining configuration surface where a misspelled key is
silently dead. Every other surface hard-errors with the key, the
surface, and the file named.

## Solution

Declare the complete config.json key set from one authority (a schema —
ideally strictspec-generated like the release file and transition record,
or at minimum a bound constant like MEMBER_KEYS), refuse unknown keys at
load with the same error shape the workspace loader uses, and flip the
config-schema check's description from "three bans" to real schema
conformance. Mind the nesting (pipelines entries, batch_limits,
external_checks entries already have per-entry validation — fold, don't
duplicate) and the releasable-level config.json override layering in
workspaces.

## Affected

rlsbl/config.py (loader + validate_config_schema), possibly a new
.strictspec schema + generated validator, docs/configuration.md (the
policed-surfaces row flips), docs/checks.md row, tests including the
four migrated fleet workspaces still loading.

## Effort

Medium-large (the key inventory across all pipelines/targets is the real
work).
