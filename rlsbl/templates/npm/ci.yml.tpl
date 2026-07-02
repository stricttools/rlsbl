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
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: ${{ matrix.node-version }}
      - name: Install dependencies
        run: |
          if [ -f package-lock.json ]; then
            npm ci
          else
            npm install
          fi
      - run: npm test --if-present
      - run: npm audit --audit-level=moderate || true
