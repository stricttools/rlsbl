+++
title = "Customizing CI workflows"
description = "Adding GitHub Actions jobs to a scaffolded project through workflow files scaffold never renders, so they never meet a template change in a three-way merge, and how a workspace's CI router picks a member's extra CI workflows up."
+++

# Customizing CI workflows

`rlsbl scaffold` renders `.github/workflows/ci.yml` and `publish.yml` and merges template changes into them on every run. Extra jobs belong in a workflow file of their own, which scaffold has no template for and never touches:

- `.github/workflows/ci-custom.yml`, running beside `ci.yml`;
- `.github/workflows/publish-custom.yml`, running beside `publish.yml`.

```yaml
name: CI (custom)

on:
  push:
  pull_request:

jobs:
  my-extra-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Custom check
        run: ./scripts/my-check.sh
```

GitHub runs every workflow file in `.github/workflows/`, so a separate file is the same as extra jobs in `ci.yml`, without the merge.

## Why not edit ci.yml?

Scaffold three-way merges template updates into the files it manages ([the three-way merge](scaffold.md#the-scaffold-state-and-the-three-way-merge)). A workflow's structure is rigid: an edit in one job can meet a template change in a neighboring one, and the merge then leaves conflict markers that fail the scaffold and block every release until resolved. A file scaffold never renders cannot conflict.

## What the release reads

- A custom CI job is part of the project's CI only when its check-run name matches the pattern the release and the `wait-for-ci` job read: the CI job names scaffold renders in a standalone repository, the member's router jobs in a workspace, or the root releasable's declared `publish_ci_check_pattern`. A job outside the pattern still runs on every push, but nothing waits for it.
- A custom publish workflow starts on the same `release: published` event as `publish.yml`, without the `wait-for-ci` job unless it declares one, so it runs whether or not the release commit's CI passed.

## In a workspace

GitHub reads workflows only at the repository root, so a member's own workflows run through the routers `rlsbl monorepo sync --auto-commit` generates. The CI router inlines a member's `ci.yml` and every `ci-*.yml`, so a member's `ci-custom.yml` is inlined like its `ci.yml`, filtered by the member's path filter and named `<member prefix> / <job>`. The publish router inlines only a member's `publish.yml`. Workflows for the whole repository go in the root's `.github/workflows/` under names the routers do not generate.
