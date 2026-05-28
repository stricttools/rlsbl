# Scaffold misses non-CI/publish files from non-primary targets

## Problem

In multi-target projects, `run_cmd_multi` (init_cmd.py:1466) only calls `template_mappings()` on the primary target (first in the targets list). Target-specific non-workflow files like `.npmignore` from non-primary targets are never scaffolded.

Example: claudestream has `targets: ["pypi", "npm"]`. Since pypi is primary, the npm target's `.npmignore` mapping is never processed. Projects with `targets: ["npm", ...]` get `.npmignore` because npm is primary.

## Root cause

Line 1466: `ci_mappings = [m for m in reg.template_mappings() if "publish" not in m["template"]]` only collects from the primary target's `template_mappings()`. Non-primary targets only contribute to the merged publish workflow via `_generate_merged_publish()`.

## Fix

Collect non-CI, non-publish target-specific files from ALL targets, not just the primary. Iterate all registries, call each one's `template_mappings()`, filter out CI and publish templates, and include the rest (like `.npmignore`).

## Affected projects

Any multi-target project where npm is not the primary target: claudestream (pypi primary), and potentially others.

## Effort

Small. The loop already exists; it just needs to iterate all targets for non-workflow files.
