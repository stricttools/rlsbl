# Release watch auto-retries Publish runs that fail deterministically

## Context

`release run --watch` re-dispatches the Publish workflow when it fails, to paper over transient CI flakes.

## Problem

Observed in a consumer project: a Publish job failed for a deterministic, config-level reason (a tool invoked with a structurally impossible setup). The watch re-dispatched the workflow once anyway; the retry failed identically. Retrying cannot help when the failure is a function of the workflow content at the tag — it costs a full CI run and delays the human diagnosis by several minutes.

## Solution

Classify before retrying. Cheap heuristics that cover most cases:

- If the failing job failed in under some small wall-clock threshold AND the failure step is the same tool invocation both times, don't retry again (already true after the first retry — but the first retry should also be gated).
- Better: fetch the failed step's log tail and match against known-deterministic signatures (config/parse/validation errors) vs transient ones (network, 5xx, rate limits). Only retry transient classes.
- At minimum: after a retry fails on the same step, print the failing step's log tail directly instead of just the run URL.

## Affected files

- The CI watch/retry logic (`rlsbl watch` / release watch integration)
- Tests with mocked run outcomes

## Effort

Small-medium depending on how far the classification goes.
