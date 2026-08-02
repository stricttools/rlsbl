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
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: 24
          registry-url: {{registryUrl}}
      - name: Verify binary asset exists
        run: |
          # Construct the release asset URL for one representative platform
          # and hard-fail on 404. Catches goreleaser asset-naming drift at
          # the release that introduced it, instead of letting broken
          # packages reach the registry.
          OWNER_REPO="${GITHUB_REPOSITORY}"
          TAG="${RELEASE_TAG}"
          ASSET_URL="https://github.com/${OWNER_REPO}/releases/download/${TAG}/${OWNER_REPO##*/}_${TAG#v}_linux_amd64.tar.gz"
          echo "Checking binary asset: ${ASSET_URL}"
          HTTP_CODE=$(curl -sSL -o /dev/null -w '%{http_code}' "${ASSET_URL}")
          if [ "${HTTP_CODE}" = "404" ]; then
            echo "::error::Binary asset not found (HTTP ${HTTP_CODE}): ${ASSET_URL}"
            echo "This usually means goreleaser asset naming changed. Check the release assets."
            exit 1
          fi
          echo "Binary asset verified (HTTP ${HTTP_CODE})"
          # The shim verifies each download against checksums.txt (goreleaser
          # emits this literal filename). If it is missing, every install would
          # fail checksum verification -- catch it here, at the release.
          CHECKSUMS_URL="https://github.com/${OWNER_REPO}/releases/download/${TAG}/checksums.txt"
          echo "Checking checksums file: ${CHECKSUMS_URL}"
          SUM_CODE=$(curl -sSL -o /dev/null -w '%{http_code}' "${CHECKSUMS_URL}")
          if [ "${SUM_CODE}" = "404" ]; then
            echo "::error::checksums.txt not found (HTTP ${SUM_CODE}): ${CHECKSUMS_URL}"
            echo "The launcher shim verifies downloads against checksums.txt; without it, installs fail."
            exit 1
          fi
          echo "Checksums file verified (HTTP ${SUM_CODE})"
        env:
          RELEASE_TAG: ${{ inputs.tag || github.event.release.tag_name }}
      - name: Install dependencies
        # Full install (devDependencies included): `npm publish` runs the
        # package's prepack script, which for a TypeScript package compiles
        # with the dev toolchain. A bare checkout has none of it.
        run: npm ci
      - name: Check if already published
        id: check-npm
        run: |
          PKG_NAME=$(node -p "require('./package.json').name")
          PKG_VERSION=$(node -p "require('./package.json').version")
          if npm view "${PKG_NAME}@${PKG_VERSION}" version 2>/dev/null; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${PKG_NAME}@${PKG_VERSION}"
          fi
      - name: Determine dist-tag
        id: dist-tag
        run: |
          PKG_VERSION=$(node -p "require('./package.json').version")
          if echo "$PKG_VERSION" | grep -q '-'; then
            PREID=$(echo "$PKG_VERSION" | sed 's/[^-]*-//' | sed 's/\.[^.]*$//')
            echo "tag=--tag $PREID" >> "$GITHUB_OUTPUT"
          else
            echo "tag=" >> "$GITHUB_OUTPUT"
          fi
      # --access public is hardcoded on purpose: scoped packages are forbidden
      # in this ecosystem, and unscoped public packages require --access public.
      # --provenance is toggled by the pipeline's `provenance` config key.
      - run: npm publish {{#if npm.provenance}}--provenance {{/if}}--access public ${{ steps.dist-tag.outputs.tag }}
        if: steps.check-npm.outputs.skip != 'true'
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
