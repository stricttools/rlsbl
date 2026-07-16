name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to publish (e.g. v1.2.3). Overrides the ref for retry dispatch."
        required: false
        type: string

# One publish run per tag: a workflow_dispatch retry at the same tag
# queues behind the in-flight run instead of racing it. A publish is never
# cancelled mid-flight.
concurrency:
  group: publish-${{ inputs.tag || github.ref_name }}
  cancel-in-progress: false

permissions:
  contents: read

jobs:
{{publishGate}}
  verify-module:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - name: Extract version from tag
        run: echo "VERSION=${GITHUB_REF_NAME#v}" >> $GITHUB_ENV
      - name: Verify module is available on proxy
        run: |
          MODULE=$(head -1 go.mod | awk '{print $2}')
          GOPROXY=proxy.golang.org go list -m "${MODULE}@v${VERSION}"
