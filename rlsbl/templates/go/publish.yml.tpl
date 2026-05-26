name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  goreleaser:
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
