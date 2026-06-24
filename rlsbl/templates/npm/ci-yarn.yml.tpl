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
{{#if npm.minRequiredNode}}        # engines.node: >= {{npm.minRequiredNode}}
{{/if}}        node-version: [20, 22, 24]
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: ${{ matrix.node-version }}
      - name: Install dependencies
        run: |
          if [ -f yarn.lock ]; then
            yarn install --frozen-lockfile
          else
            yarn install
          fi
      - run: yarn test
      - run: yarn audit --audit-level=moderate || true
