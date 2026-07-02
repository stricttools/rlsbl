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
      # minimum_zig_version: {{zig.minRequiredZig}}
      - uses: {{action "mlugg/setup-zig"}}
        with:
          version: {{zig.minRequiredZig}}
      - run: zig build
      - run: zig build test
