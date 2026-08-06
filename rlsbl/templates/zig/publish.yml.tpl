name: Publish

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

permissions:
  contents: write

jobs:
{{publishGate}}
  build-and-upload:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "mlugg/setup-zig"}}
        with:
          version: {{zig.minRequiredZig}}
      # No secret scan here: this workflow uploads bare cross-compiled
      # binaries, never a packed archive, so there is no textual artifact to
      # scan before the upload. The scan this workflow used to run was a
      # whole-tree `gitleaks dir .`, which flags files that never ship and has
      # blocked a release mid-flight. Artifact-scoped scanning is the rule;
      # scanning the tree is not an acceptable stand-in.
      - name: Check if already published
        id: check-zig
        run: |
          TAG="${RELEASE_TAG}"
          RELEASE_ASSETS=$(gh release view "${TAG}" --json assets -q '.assets | length' 2>/dev/null || echo "0")
          if [ "${RELEASE_ASSETS}" -gt 0 ]; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${TAG} (${RELEASE_ASSETS} assets)"
          fi
        env:
          RELEASE_TAG: ${{ inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Build x86_64-linux
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-x86_64-linux

      - name: Build aarch64-linux
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-linux
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-aarch64-linux

      - name: Build x86_64-macos
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-macos
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-x86_64-macos

      - name: Build aarch64-macos
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-aarch64-macos

      - name: Build x86_64-windows
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-windows
          cp zig-out/bin/{{zig.projectName}}.exe {{zig.projectName}}-x86_64-windows.exe

      - name: Build aarch64-windows
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-windows
          cp zig-out/bin/{{zig.projectName}}.exe {{zig.projectName}}-aarch64-windows.exe

      - name: Upload release assets
        if: steps.check-zig.outputs.skip != 'true'
        run: |
          gh release upload "${{ inputs.tag || github.ref_name }}" \
            {{zig.projectName}}-x86_64-linux \
            {{zig.projectName}}-aarch64-linux \
            {{zig.projectName}}-x86_64-macos \
            {{zig.projectName}}-aarch64-macos \
            {{zig.projectName}}-x86_64-windows.exe \
            {{zig.projectName}}-aarch64-windows.exe
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
{{npmPublishJobs}}
