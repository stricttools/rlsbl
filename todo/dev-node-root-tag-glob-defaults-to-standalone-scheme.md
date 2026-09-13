# A target-less dev-node root member's tag glob defaults to the standalone `v*` and trips the empty-record refusal

## Context

A workspace's root member may be a dev node (`dev_only = true`, `releasable = false`,
no release target). Some workspaces also carry bare `v0.x.y` tags from before
the monorepo conversion, recorded as `shipped_as` spellings on one releasable's
archives.

## Problem

`resolve_release_scope` (`rlsbl/context.py` around 263-271) leaves `tag_glob`
unset when `detect_targets` finds nothing for the member, and `_scheme_tags`
(`rlsbl/release_record.py` around 308) then defaults the unset glob to the
standalone `v*`. At the root that glob sweeps up the historical bare tags,
and `_require_backfilled_release_record` fires against the root's empty
`.rlsbl/releases` from `rlsbl unreleased` and `rlsbl watch`: "the release
record is empty, but this repository has version tags". `rlsbl release
backfill` reports nothing to do, because every archive is already recorded on
the releasable that owns those tags, so the message names a remedy that cannot
clear it.

`rlsbl/checks/workspace.py::_member_tag_glob` handles the same case correctly
by falling back to `<name>@v*`, which matches nothing for `root`. `cmd_status`
refuses at a bare workspace root through `_refuse_status_at_bare_workspace_root`;
`unreleased` and `watch` have no such refusal and fall into the record read.

## Solutions

1. Make `_scheme_tags` (or the scope resolution) use the same fallback as the
   workspace check, `<member name>@v*`, when the member has no target, so a
   dev-node root owns no tags. Red-green with a fixture carrying bare tags
   recorded as another releasable's `shipped_as`. Recommended.
2. Give `unreleased` and `watch` the bare-workspace-root refusal that `status`
   has, naming the member directories to run from. Complements 1.

## Affected files

`rlsbl/context.py`, `rlsbl/release_record.py`, the `unreleased` and `watch`
commands, their tests.

## Effort

Small.
