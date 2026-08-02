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
      - uses: {{action "astral-sh/setup-uv"}}
      - name: Verify binary asset exists
        run: |
          # Construct the release asset URL for one representative platform
          # and hard-fail on 404. Catches goreleaser asset-naming drift at
          # the release that introduced it, instead of letting broken
          # packages reach the registry.
          OWNER_REPO="${GITHUB_REPOSITORY}"
          TAG="${RELEASE_TAG}"
          # Baked in at scaffold time from the WRAPPED PRODUCER pipeline --
          # the same two values the launcher shim downloads with, so the probe
          # can never check a different URL than users fetch. The repo
          # basename is not the asset project name (goreleaser names assets
          # after the producer), and a bare "v" strip leaves a prefixed
          # monorepo tag ("<name>@v1.2.3") completely intact.
          ASSET_PROJECT="{{assetProject}}"
          TAG_PREFIX="{{tagPrefix}}"
          VERSION="${TAG#"${TAG_PREFIX}"}"
          ASSET_URL="https://github.com/${OWNER_REPO}/releases/download/${TAG}/${ASSET_PROJECT}_${VERSION}_linux_amd64.tar.gz"
          echo "Checking binary asset: ${ASSET_URL}"
          HTTP_CODE=$(curl -sSL -o /dev/null -w '%{http_code}' "${ASSET_URL}")
          if [ "${HTTP_CODE}" = "404" ]; then
            echo "::error::Binary asset not found (HTTP ${HTTP_CODE}): ${ASSET_URL}"
            echo "This usually means goreleaser asset naming changed. Check the release assets."
            exit 1
          fi
          echo "Binary asset verified (HTTP ${HTTP_CODE})"
          # The shim verifies each download against checksums.txt (goreleaser
          # emits this literal filename). If it is missing, every first run
          # would fail checksum verification -- catch it here, at the release.
          CHECKSUMS_URL="https://github.com/${OWNER_REPO}/releases/download/${TAG}/checksums.txt"
          echo "Checking checksums file: ${CHECKSUMS_URL}"
          SUM_CODE=$(curl -sSL -o /dev/null -w '%{http_code}' "${CHECKSUMS_URL}")
          if [ "${SUM_CODE}" = "404" ]; then
            echo "::error::checksums.txt not found (HTTP ${SUM_CODE}): ${CHECKSUMS_URL}"
            echo "The launcher shim verifies downloads against checksums.txt; without it, first runs fail."
            exit 1
          fi
          echo "Checksums file verified (HTTP ${SUM_CODE})"
        env:
          RELEASE_TAG: ${{ inputs.tag || github.event.release.tag_name }}
      - run: uv build --out-dir dist
      - uses: {{action "pypa/gh-action-pypi-publish"}}
        with:
          skip-existing: true
