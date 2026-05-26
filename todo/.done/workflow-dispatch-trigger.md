# Add workflow_dispatch trigger to scaffold-generated workflows

## Problem

When GitHub Actions experiences outages or event processing failures, push and release events are delivered but never processed into workflow runs. Once the outage resolves, missed events are NOT retroactively replayed. This leaves releases unpublished on PyPI/npm and CI unverified.

This happened during the claudestream v0.7.0 release (2026-05-26) — GitHub Actions incident `gnftqj9htp0g` caused authentication failures that prevented all Actions runs from starting. The release commits were pushed, the tag and GitHub Release were created, but no CI or Publish workflow ran.

Recovery required manually deleting and re-creating the GitHub Release, plus pushing an empty commit to re-trigger CI. There was no way to manually dispatch the workflows because they lacked the `workflow_dispatch` trigger.

## Proposed solution

Add `workflow_dispatch` to both scaffold-generated workflow files:

**CI workflow** (`.github/workflows/ci.yml`):
```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:
```

**Publish workflow** (`.github/workflows/publish.yml`):
```yaml
on:
  release:
    types: [published]
  workflow_dispatch:
```

This enables manual triggering via `gh workflow run CI --ref main` and `gh workflow run Publish --ref v0.7.0` when automated triggers fail.

## Additional consideration

`rlsbl watch` could detect the "no runs found" case and suggest `gh workflow run` as a remediation step, instead of just timing out.

## Affected files

- Workflow templates in rlsbl's scaffold system (the templates that `rlsbl scaffold` generates)
- `rlsbl watch` command (optional improvement for better error messages)

## Effort

Small. One line added to each workflow template. The `rlsbl watch` improvement is a nice-to-have.
