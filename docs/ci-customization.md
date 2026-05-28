---
title: Customizing CI workflows
description: "Add custom GitHub Actions jobs to scaffold-managed projects via ci-custom.yml and publish-custom.yml without conflicting with three-way merge updates."
---

# Customizing CI workflows

`rlsbl scaffold` generates `.github/workflows/ci.yml` and `publish.yml` and updates them on re-run. To add custom jobs without fighting merge conflicts, use a separate workflow file:

- `.github/workflows/ci-custom.yml` -- runs on push/PR like ci.yml
- `.github/workflows/publish-custom.yml` -- runs on release like publish.yml

Scaffold never creates or modifies these files. They are entirely owned by you.

Example `ci-custom.yml`:

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

These workflows run alongside the scaffold-managed ones -- no merging required.

## Why not edit ci.yml directly?

`scaffold` uses a three-way merge (`git merge-file`) to apply template updates to your local workflow files. For most files this works well, but YAML workflows have rigid structure: a customization in one job can collide with an upstream change in a neighboring job, producing conflict markers that block the auto-commit.

Custom workflow files sidestep this entirely. GitHub Actions runs every `.yml` file in `.github/workflows/`, so a separate file is functionally equivalent to extra jobs in `ci.yml` -- but scaffold has no template for it and will never touch it.

When `scaffold` does hit a conflict on `ci.yml` or `publish.yml`, it prints a tip pointing at this pattern.
