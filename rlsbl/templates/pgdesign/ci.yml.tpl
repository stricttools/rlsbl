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
      # The Go that installs pgdesign is declared once, in .go-version next to
      # pgdesign.toml (one line, e.g. 1.26.6): the same Go as local development.
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: .go-version
      # @v0: a phantom v1.0.0 is permanently cached on the Go module proxy; @latest resolves it
      - run: go install github.com/smm-h/pgdesign/cmd/pgdesign@v0
      - run: pgdesign check --tag validation
