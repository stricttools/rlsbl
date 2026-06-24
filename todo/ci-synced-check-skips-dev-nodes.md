# workspace-ci-synced check should skip dev_node projects

## Problem

`workspace-ci-synced` expects `{name}-ci.yml` for every project in workspace.toml, including dev_node projects. Dev node projects (like an npm shim package with no tests or code) may have no `.github/workflows/ci.yml` source template, so scaffold never generates a root-level CI workflow for them. The check then fails with "missing workflows: npm".

## Root cause

The check at `checks/workspace.py` iterates `ctx.projects` without filtering out dev_node projects. The `non_dev_node` scope filter exists in `checks/scope.py` (line 60-64) but is not applied to this check.

## Fix

Apply the `non_dev_node` scope filter to the `workspace-ci-synced` check registration. Dev node projects are leaf nodes that cannot be released and whose changes don't affect users — requiring CI workflows for them adds no value.
