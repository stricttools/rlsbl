name: Docker Publish

on:
  release:
    types: [published]
  workflow_dispatch:

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "docker/login-action"}}
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: {{action "docker/metadata-action"}}
        id: meta
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=semver,pattern=\{{version}}
            type=raw,value=latest
      - uses: {{action "docker/build-push-action"}}
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          build-args: VERSION=${{ github.event.release.tag_name }}
