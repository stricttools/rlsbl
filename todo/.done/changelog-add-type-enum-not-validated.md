# `changelog add` does not validate the `--type` enum

## Context

The JSONL entry schema defines `type` as one of `feature`, `fix`, `breaking`. Documentation for `changelog add` states it "validates the schema" before appending.

## Problem

`rlsbl changelog add --commits <hash> --description "..." --type <anything>` accepts arbitrary type strings. Observed in a consumer project: an invalid type (a typo'd multi-word value) was accepted and written into `unreleased.jsonl` without complaint. The invalid entry is only caught later — by `rlsbl check --tag changelog` schema conformance or at release time — far from the point of the mistake, and after the entry file was auto-committed.

This contradicts the tool's own hard-errors-at-the-boundary philosophy: `add` resolves commit hashes eagerly and errors on bad hashes, but not on bad types.

## Solution

Validate `--type` against the enum at registration/parse time in `changelog add` (and `changelog edit`/`amend`, which should be checked for the same gap) with a hard error listing the allowed values. Red-green: a test asserting `add` with an invalid type exits non-zero and writes nothing.

## Affected files

- The `changelog add` implementation (and `edit`/`amend` siblings) under `rlsbl/commands/`
- Their test files

## Effort

Small — an hour including tests for all three subcommands.
