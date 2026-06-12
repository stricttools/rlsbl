name: Publish

on:
  release:
    types: [published]
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "pnpm/action-setup"}}
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
      - run: pnpm publish --provenance --access public
        if: steps.check-npm.outputs.skip != 'true'
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
