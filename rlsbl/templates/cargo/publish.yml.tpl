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
  id-token: write

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
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "dtolnay/rust-toolchain"}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
      - name: Check if already published
        id: check-cargo
        run: |
          PKG_NAME=$(grep '^name' Cargo.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
          PKG_VERSION=$(grep '^version' Cargo.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
          if curl -sf "https://crates.io/api/v1/crates/${PKG_NAME}/${PKG_VERSION}" -H "User-Agent: rlsbl-ci" > /dev/null 2>&1; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${PKG_NAME}@${PKG_VERSION}"
          fi
      - name: Authenticate with crates.io
        if: steps.check-cargo.outputs.skip != 'true'
        uses: {{action "rust-lang/crates-io-auth-action"}}
        id: crates-auth
      - run: cargo publish
        if: steps.check-cargo.outputs.skip != 'true'
        env:
          CARGO_REGISTRY_TOKEN: ${{ steps.crates-auth.outputs.token }}
