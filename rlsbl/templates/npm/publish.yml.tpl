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
      - run: npm publish --provenance --access public ${{ steps.dist-tag.outputs.tag }}
        if: steps.check-npm.outputs.skip != 'true'
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
