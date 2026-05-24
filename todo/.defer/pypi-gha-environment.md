# GitHub Actions environment for Trusted Publishing

## Problem

PyPI's Trusted Publisher configuration has an optional "environment name" field. When set, the OIDC token is only issued when the workflow runs in that specific GitHub Actions environment. Without it, any workflow run in the repo can mint a token and publish.

## What environments provide

- Branch restrictions (only deploy from main)
- Required reviewers before deployment
- Scoped secrets (secrets only available in that environment)
- Access control for multi-maintainer repos where not everyone should publish

## What rlsbl would need

- Optional `publish.pypi.environment` in `.rlsbl/config.json`
- Scaffold emits `environment: <name>` in the publish workflow job when set
- Documentation: user must create the environment in GitHub (Settings > Environments) and add it to the PyPI trusted publisher config

## Assessment

Small lift. Only matters for multi-maintainer repos. Solo maintainers gain nothing. Could generalize to `publish.<target>.environment` since GitHub Actions environments scope secrets for any target.
