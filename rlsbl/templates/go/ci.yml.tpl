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
    steps:
      - uses: {{action "actions/checkout"}}
{{#if go.minRequiredGo}}      # go.mod: go {{go.minRequiredGo}}
{{/if}}      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - run: go vet ./...
      - run: go test ./... -race -short -timeout=10m
