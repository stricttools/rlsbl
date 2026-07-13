# Scaffolded publish workflow assumes every Go target is a binary

## Context

The publish.yml scaffold's Go job runs goreleaser with no config. For Go *library* modules (no main package), the pushed tag IS the publish — there is nothing to build or upload.

## Problem

Observed in a consumer project with a Go library target: the Publish workflow's go job failed with goreleaser's "build ... does not contain a main function", twice (initial + auto-retry), even though npm/pypi jobs succeeded and the Go module was already fully published by the tag. The workflow reports failure for a release that is actually complete, and the failure mode is only discoverable by reading goreleaser internals.

## Solution

The scaffold should distinguish Go library vs binary pipelines — e.g. a required config key on Go pipelines (`"artifact": "library" | "binary"`, no default, per the no-implicit-defaults policy):

- binary: goreleaser as today
- library: a verification step instead — `go mod download <module>@<tag>` against proxy.golang.org from a temp module, hard-fail if the proxy cannot serve it

The consumer project now carries exactly that verification step as a local customization; the three-way merge must preserve it on re-scaffold, but the pattern belongs in the scaffold itself.

## Affected files

- publish workflow template(s) under the scaffold templates
- Go pipeline config schema/validation
- scaffold tests

## Effort

Small-medium — template branch + config key + validation + tests.
