name: CI

on:
  push:
    branches: [main]
  pull_request:
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
      - uses: {{action "actions/setup-go"}}
        with:
          go-version: stable
      # @v0: a phantom v1.0.0 is permanently cached on the Go module proxy; @latest resolves it
      - run: go install github.com/smm-h/pgdesign/cmd/pgdesign@v0
      - run: pgdesign check --tag validation
