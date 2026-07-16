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

permissions:
  contents: write

jobs:
{{publishGate}}
  goreleaser:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          fetch-depth: 0
      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
      - name: Check if already published
        id: check-go
        run: |
          TAG="${GITHUB_REF_NAME}"
          if git ls-remote --tags origin "${TAG}" | grep -q "${TAG}"; then
            # Tag already pushed and goreleaser assets likely exist
            RELEASE_ASSETS=$(gh release view "${TAG}" --json assets -q '.assets | length' 2>/dev/null || echo "0")
            if [ "${RELEASE_ASSETS}" -gt 0 ]; then
              echo "skip=true" >> "$GITHUB_OUTPUT"
              echo "Already published: ${TAG} (${RELEASE_ASSETS} assets)"
            fi
          fi
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - uses: {{action "goreleaser/goreleaser-action"}}
        if: steps.check-go.outputs.skip != 'true'
        with:
          version: "~> v2"
          args: release --clean
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
{{homebrewEnv}}
{{npmPublishJobs}}
{{cratesPublishJobs}}
