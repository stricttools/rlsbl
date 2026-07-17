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

# Private-module posture: a private Go module cannot be verified here. The
# public module proxy (proxy.golang.org) refuses to serve private modules, so
# this verification step would always fail. Private Go libraries must set
# publish_mode "none" in .rlsbl/config.json -- that suppresses this publish
# job entirely (no workflow is scaffolded). Do NOT keep this job for a private
# module.

jobs:
{{publishGate}}
  verify-module:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.ref_name }}
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - name: Verify module is available on proxy
        run: |
          # The module path is baked at scaffold time from this module's
          # go.mod. It is the full import path -- including the monorepo
          # subdirectory when the module lives in one -- so `go list -m` asks
          # the proxy for the right module. The proxy resolves subdir modules
          # via their companion subdir tag (<subdir>/vX.Y.Z) automatically, so
          # no manual tag construction is needed here.
          MODULE="{{modulePath}}"

          # Resolve the release tag with the same fallback the checkout uses,
          # then normalise it to a bare version. Three tag shapes reach this
          # step and all reduce to the trailing vX.Y.Z:
          #   - plain standalone:        v0.22.0
          #   - releasable-format:       go-strictcli@v0.22.0
          #   - monorepo subdir member:  <subdir>/v0.22.0
          # Strip the releasable "<name>@" prefix, then the subdir "<path>/"
          # prefix, then the leading "v".
          TAG="${{ inputs.tag || github.ref_name }}"
          TAG="${TAG##*@}"
          TAG="${TAG##*/}"
          VERSION="${TAG#v}"

          GOPROXY=proxy.golang.org go list -m "${MODULE}@v${VERSION}"
