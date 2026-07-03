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

jobs:
{{publishGate}}
  publish:
    needs: gate
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-java"}}
        with:
          distribution: temurin
          java-version: "25"
      - uses: {{action "gradle/actions/setup-gradle"}}
      # GitHub Packages allows re-publishing the same version (overwrites),
      # so this step is inherently idempotent -- no pre-check needed.
      - run: ./gradlew publish
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
