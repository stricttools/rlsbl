name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "denoland/setup-deno"}}
        with:
          deno-version: v2.x
      - run: deno publish
