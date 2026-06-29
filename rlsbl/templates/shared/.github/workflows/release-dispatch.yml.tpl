name: Release

on:
  workflow_dispatch:
    inputs:
      bump:
        description: 'Bump type'
        required: true
        type: choice
        options:
          - patch
          - minor
          - major
          - prerelease
          - hotfix
      description:
        description: 'Release description'
        required: true
        type: string
      preid:
        description: 'Pre-release identifier (none for stable release)'
        required: true
        type: choice
        default: 'none'
        options:
          - none
          - alpha
          - beta
          - rc
          - stable

permissions:
  contents: write
  id-token: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          fetch-depth: 0
          token: ${{ secrets.RELEASE_TOKEN }}

      - uses: {{action "astral-sh/setup-uv"}}

      - name: Install rlsbl
        run: uv tool install rlsbl

      - name: Release
        run: |
          PREID_FLAG=""
          if [ "${{ inputs.preid }}" != "none" ]; then
            PREID_FLAG="--preid ${{ inputs.preid }}"
          fi
          rlsbl release run \
            --bump "${{ inputs.bump }}" \
            --description "${{ inputs.description }}" \
            $PREID_FLAG \
            --yes --no-watch
        env:
          GH_TOKEN: ${{ secrets.RELEASE_TOKEN }}
