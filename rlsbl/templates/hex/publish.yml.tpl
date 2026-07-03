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
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "erlef/setup-beam"}}
        with:
          otp-version: "26"
          elixir-version: "1.16"
      - run: mix deps.get
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
