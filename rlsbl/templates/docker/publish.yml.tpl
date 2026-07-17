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
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
      - name: Check if already published
        id: check-docker
        run: |
          IMAGE="${REGISTRY}/${IMAGE_NAME}"
          TAG="${GITHUB_REF_NAME#v}"
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
