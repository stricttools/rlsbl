name: Release Finalize

on:
  pull_request:
    types: [closed]
    branches: [main]

permissions:
  contents: write
  actions: write

jobs:
  finalize:
    if: >-
      github.event.pull_request.merged == true &&
      startsWith(github.event.pull_request.head.ref, 'release/')
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          fetch-depth: 0

      - name: Read release metadata
        id: meta
        run: |
          PENDING=".rlsbl/releases/pending.json"
          if [ ! -f "$PENDING" ]; then
            echo "No pending.json found, skipping"
            echo "skip=true" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          echo "version=$(jq -r .version "$PENDING")" >> "$GITHUB_OUTPUT"
          echo "tag=$(jq -r .tag "$PENDING")" >> "$GITHUB_OUTPUT"
          echo "skip=false" >> "$GITHUB_OUTPUT"

      - name: Create tag
        if: steps.meta.outputs.skip != 'true'
        run: |
          git tag ${{ steps.meta.outputs.tag }}
          git push origin ${{ steps.meta.outputs.tag }}

      - name: Create companion tags
        if: steps.meta.outputs.skip != 'true'
        run: |
          PENDING=".rlsbl/releases/pending.json"
          for ctag in $(jq -r '.companion_tags[]' "$PENDING" 2>/dev/null); do
            git tag "$ctag"
            git push origin "$ctag"
          done

      - name: Create GitHub Release
        if: steps.meta.outputs.skip != 'true'
        run: |
          PENDING=".rlsbl/releases/pending.json"
          jq -r .changelog_entry "$PENDING" > /tmp/release-notes.md
          PRERELEASE_FLAG=""
          if echo "${{ steps.meta.outputs.version }}" | grep -qE "-(alpha|beta|rc)\."; then
            PRERELEASE_FLAG="--prerelease"
          fi
          gh release create "${{ steps.meta.outputs.tag }}" \
            --title "${{ steps.meta.outputs.tag }}" \
            --notes-file /tmp/release-notes.md \
            $PRERELEASE_FLAG
        env:
          GH_TOKEN: ${{ github.token }}

      - name: Dispatch publish workflows
        if: steps.meta.outputs.skip != 'true'
        run: |
          PENDING=".rlsbl/releases/pending.json"
          TAG="${{ steps.meta.outputs.tag }}"
          for workflow in $(jq -r '.dispatch[]' "$PENDING"); do
            echo "Dispatching $workflow for $TAG"
            gh workflow run "$workflow" --ref "$TAG"
          done
        env:
          GH_TOKEN: ${{ github.token }}

      - name: Clean up pending metadata
        if: steps.meta.outputs.skip != 'true'
        run: |
          rm .rlsbl/releases/pending.json
          git add .rlsbl/releases/pending.json
          git commit -m "chore: clean up release metadata for ${{ steps.meta.outputs.tag }}"
          git push
