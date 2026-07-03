name: Docker Publish

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

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
{{publishGate}}
  publish:
    needs: gate
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
            type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}
      - uses: {{action "docker/build-push-action"}}
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          build-args: VERSION=${{ github.ref_name }}
