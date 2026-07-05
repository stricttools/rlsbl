name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

# Per-SHA group: re-runs of the same commit dedupe, but a new commit never
# cancels an earlier commit's in-flight run (release CI conclusions stay intact).
concurrency:
  group: ${{ github.workflow_ref }}-${{ github.sha }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
{{#if npm.minRequiredNode}}        # engines.node: >= {{npm.minRequiredNode}}
{{/if}}        node-version: [20, 22, 24]
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "pnpm/action-setup"}}
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: ${{ matrix.node-version }}
      - name: Install dependencies
        run: |
          if [ -f pnpm-lock.yaml ]; then
            pnpm install --frozen-lockfile
          elif [ -f "$GITHUB_WORKSPACE/pnpm-lock.yaml" ]; then
            cd "$GITHUB_WORKSPACE" && pnpm install --frozen-lockfile --filter {{npm.name}}
          else
            echo "::error::No pnpm-lock.yaml found in project dir or repo root" && exit 1
          fi
      - run: pnpm test
      - run: pnpm audit --audit-level=moderate || true
