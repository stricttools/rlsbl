name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

# One publish run per tag ref: a workflow_dispatch retry at the same tag
# queues behind the in-flight run instead of racing it. A publish is never
# cancelled mid-flight.
concurrency:
  group: ${{ github.workflow_ref }}-${{ github.ref }}
  cancel-in-progress: false

permissions:
  contents: write

jobs:
{{publishGate}}
  goreleaser:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          fetch-depth: 0
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - uses: {{action "goreleaser/goreleaser-action"}}
        with:
          version: "~> v2"
          args: release --clean
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
{{homebrewEnv}}
{{npmPublishJobs}}
