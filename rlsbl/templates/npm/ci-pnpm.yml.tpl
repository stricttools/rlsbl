name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
{{#if minRequiredNode}}        # engines.node: >= {{minRequiredNode}}
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
          else
            pnpm install
          fi
      - run: pnpm test
      - run: pnpm audit --audit-level=moderate || true
