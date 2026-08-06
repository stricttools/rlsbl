name: Docker Publish

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
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      # No secret scan here: build-push-action builds and pushes the image in
      # one step, so there is no pre-publish artifact on disk to scan. The
      # scan this workflow used to run was a whole-tree `gitleaks dir .`,
      # which flags files that never ship and has blocked a release
      # mid-flight. Artifact-scoped scanning is the rule; scanning the tree is
      # not an acceptable stand-in.
      - name: Check if already published
        id: check-docker
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
        run: |
          IMAGE="${REGISTRY}/${IMAGE_NAME}"
          TAG="${RELEASE_TAG#v}"
          if docker manifest inspect "${IMAGE}:${TAG}" > /dev/null 2>&1; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${IMAGE}:${TAG}"
          fi
      - uses: {{action "docker/login-action"}}
        if: steps.check-docker.outputs.skip != 'true'
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: {{action "docker/metadata-action"}}
        if: steps.check-docker.outputs.skip != 'true'
        id: meta
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=semver,pattern=\{{version}}
            type=raw,value=latest,enable=${{ !contains(inputs.tag || github.ref_name, '-') }}
      - uses: {{action "docker/build-push-action"}}
        if: steps.check-docker.outputs.skip != 'true'
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          build-args: VERSION=${{ inputs.tag || github.ref_name }}
