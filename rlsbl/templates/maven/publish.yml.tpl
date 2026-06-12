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
      packages: write
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-java"}}
        with:
          distribution: temurin
          java-version: "21"
      - uses: {{action "gradle/actions/setup-gradle"}}
      # GitHub Packages allows re-publishing the same version (overwrites),
      # so this step is inherently idempotent -- no pre-check needed.
      - run: ./gradlew publish
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
