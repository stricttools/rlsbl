# Unify the batch release-file reader onto the schema authority

## Context

The single-project release file (.rlsbl/releases/unreleased.toml and the
archived v*.toml files) is validated by the strictspec-generated
release-file validator — the single shape authority, with the three-state
fate rule layered in the reader. The BATCH release file
(.rlsbl-monorepo/releases/unreleased.toml, one [releasables.<name>]
section per releasable) has its own hand-rolled reader that validates
shape ad hoc.

## Problem

Two readers, one document family. The batch reader re-implements shape
judgment the schema authority already owns, so the two can drift: a key
the schema learns (as the fate model did) is invisible to the batch
reader until someone mirrors it by hand, and batch-file error messages
are not the validator's messages.

## Solution

Give the batch file its own strictspec schema (or extend the release-file
schema with the batch document form) and generate its validator with the
same exactly-pinned toolchain; the batch reader keeps only what the
schema cannot see (releasable-name resolution against the workspace,
cross-section rules). Same pattern as the changelog-entry and
transition-record documents.

## Affected

The batch release-file reader (rlsbl/release_file.py or the monorepo
batch module — locate its parser), a new/extended .strictspec schema, a
regenerated validator, the monorepo release init/run tests.

## Effort

Medium.
