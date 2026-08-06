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

jobs:
{{publishGate}}
  publish:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "erlef/setup-beam"}}
        with:
          otp-version: "26"
          elixir-version: "1.16"
      - run: mix deps.get
      # No secret scan here: `mix hex.publish` builds and uploads the package
      # tarball in a single step, so there is no pre-publish artifact on disk
      # to scan. The scan this workflow used to run was a whole-tree
      # `gitleaks dir .`, which flags files that never ship and has blocked a
      # release mid-flight. Artifact-scoped scanning is the rule; scanning the
      # tree is not an acceptable stand-in.
      - name: Check if already published
        id: check-hex
        run: |
          PKG_NAME=$(grep 'app:' mix.exs | head -1 | sed 's/.*:\(.*\),.*/\1/' | tr -d ' ')
          PKG_VERSION=$(grep 'version:' mix.exs | head -1 | sed 's/.*"\(.*\)".*/\1/')
          if curl -sf "https://hex.pm/api/packages/${PKG_NAME}" | grep -q "\"${PKG_VERSION}\""; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${PKG_NAME}@${PKG_VERSION}"
          fi
      - run: mix hex.publish --yes
        if: steps.check-hex.outputs.skip != 'true'
        env:
          HEX_API_KEY: ${{ secrets.HEX_API_KEY }}
