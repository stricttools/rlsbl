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
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "docker/setup-buildx-action"}}
      - uses: {{action "docker/build-push-action"}}
        with:
          context: .
          push: false
          cache-from: type=gha
          cache-to: type=gha,mode=max
