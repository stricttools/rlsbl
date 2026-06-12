# Make publish workflow idempotent for safe retries

## Problem

When a publish workflow partially fails (e.g., pypi succeeds but npm fails due to transient DNS timeout), retrying the workflow causes the already-succeeded job to fail because it tries to re-publish an existing version. This happened with claudewheel v0.10.0:

- Run 1: npm succeeded, pypi failed (DNS timeout pulling Docker image)
- Run 2 (retry): npm failed ("cannot publish over previously published versions"), pypi succeeded

The net result was correct (both packages published), but the workflow showed as failed on every individual run.

## Proposed fix

Make both publish jobs idempotent so retries are safe:

### npm job
Before `npm publish`, check if the version already exists:
```yaml
- name: Check if already published
  id: check
  run: |
    VERSION=$(node -p "require('./package.json').version")
    if npm view ${{ github.event.repository.name }}@$VERSION version 2>/dev/null; then
      echo "skip=true" >> $GITHUB_OUTPUT
    fi
- name: Publish to npm
  if: steps.check.outputs.skip != 'true'
  run: npm publish --access public
```

### pypi job
Set `skip-existing: true` on the pypi publish action, OR add a similar pre-check against the PyPI API.

## Scope

This is a scaffold template change — affects `.rlsbl/templates/publish/` (or wherever the publish workflow template lives). All projects get the fix on next `rlsbl scaffold`.
