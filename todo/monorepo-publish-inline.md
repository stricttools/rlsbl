# Inline publish steps in monorepo publish router (fix PyPI Trusted Publishing)

## Problem

PyPI Trusted Publishing rejects OIDC tokens from reusable workflows (`workflow_call`). The monorepo publish router calls per-project publish workflows via `workflow_call`, so the token's `GITHUB_WORKFLOW_REF` points to the reusable workflow file (e.g., `strictcli-publish.yml`), not the router (`publish.yml`). PyPI requires the ref to match the configured workflow filename (`publish.yml`).

This has been broken since the monorepo was introduced. strictcli versions 0.6.0 through 0.7.1 never reached PyPI.

## Constraint

Each sub-project's `<project>/.github/workflows/publish.yml` must work standalone -- if the project is extracted to its own repo, the workflow should function without changes.

## Solution

The monorepo publish router (`.github/workflows/publish.yml`) should inline each sub-project's publish steps as conditional jobs instead of calling reusable workflows. The router stays named `publish.yml`, the OIDC token references `publish.yml`, and Trusted Publishing works.

Changes:
- Per-project `publish.yml` changes from `on: workflow_call` to `on: release` (standalone-ready)
- `rlsbl monorepo sync` generates the root `publish.yml` by reading each per-project publish workflow, inlining jobs with `if: startsWith(tag, '<pattern>')` and `defaults.run.working-directory: <project-path>` added
- Root `publish.yml` is auto-generated (DO NOT EDIT comment, chmod 444)
- The CI router stays unchanged (workflow_call is fine for CI -- no OIDC needed)

## Affected code

- `rlsbl/commands/monorepo.py` -- `_generate_publish_router()` function
- Per-project publish workflow templates
