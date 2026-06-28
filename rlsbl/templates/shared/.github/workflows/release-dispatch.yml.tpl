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
      description:
        description: 'Release description'
        required: true
        type: string

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
          rlsbl release run \
            --bump "${{ inputs.bump }}" \
            --description "${{ inputs.description }}" \
            --yes --no-watch
        env:
          GH_TOKEN: ${{ secrets.RELEASE_TOKEN }}
