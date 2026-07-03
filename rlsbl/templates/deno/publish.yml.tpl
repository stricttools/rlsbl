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
      id-token: write
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "denoland/setup-deno"}}
        with:
          deno-version: v2.x
      - name: Check if already published
        id: check-deno
        run: |
          PKG_NAME=$(grep '"name"' deno.json | head -1 | sed 's/.*"\(@[^"]*\)".*/\1/')
          PKG_VERSION=$(grep '"version"' deno.json | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
          if curl -sf "https://jsr.io/${PKG_NAME}/${PKG_VERSION}/meta.json" > /dev/null 2>&1; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${PKG_NAME}@${PKG_VERSION}"
          fi
      - run: deno publish
        if: steps.check-deno.outputs.skip != 'true'
