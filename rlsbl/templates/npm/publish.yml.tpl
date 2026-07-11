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
  contents: read
  id-token: write

jobs:
{{publishGate}}
  publish:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-node"}}
        with:
          node-version: 24
          registry-url: {{registryUrl}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
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
      - run: npm publish {{#if provenance}}--provenance {{/if}}--access public ${{ steps.dist-tag.outputs.tag }}
        if: steps.check-npm.outputs.skip != 'true'
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
