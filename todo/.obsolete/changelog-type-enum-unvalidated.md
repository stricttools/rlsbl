# Changelog entry `type` enum unvalidated

## Context

Found during a code audit (2026-07) of JSONL changelog entry handling.

## Problem

The `type` field of a user-facing changelog entry is documented as an enum
(`feature` / `fix` / `breaking`), but the value is not validated on read or write — a typo'd
or invented type (`"faeture"`, `"chore"`) is accepted silently. Downstream grouping (the
generated CHANGELOG sections) then misbehaves quietly: entries with unknown types are dropped,
misgrouped, or crash rendering, depending on the code path.

## Suggested fix

1. Red test: `changelog add` with an invalid `--type`, and validation over a JSONL file
   containing an invalid `type`, must both hard-error naming the allowed values.
2. Enforce the enum in every path that reads or writes entries: `changelog add`, the
   validation checks (`check --tag changelog`), and generation.
3. Scan finalized version files for already-persisted invalid values and decide the remediation
   (amend vs documented exemption) — the check must not brick released history without a
   story.

## Affected area

JSONL changelog schema/validation; `changelog add`; changelog generation.

## Effort

Small for the enum check itself; the released-history scan and remediation decision is the
part needing care.
