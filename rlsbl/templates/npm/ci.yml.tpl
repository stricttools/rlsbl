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
{{#if minRequiredNode}}        # engines.node: >= {{npm.minRequiredNode}}
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
