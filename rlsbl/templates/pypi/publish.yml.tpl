name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "astral-sh/setup-uv"}}
      - run: uv build --out-dir dist
      - uses: {{action "pypa/gh-action-pypi-publish"}}
        with:
          skip-existing: true
