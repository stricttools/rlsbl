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
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
      - name: Check if already published
        id: check-zig
        run: |
          TAG="${GITHUB_REF_NAME}"
          RELEASE_ASSETS=$(gh release view "${TAG}" --json assets -q '.assets | length' 2>/dev/null || echo "0")
          if [ "${RELEASE_ASSETS}" -gt 0 ]; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
            echo "Already published: ${TAG} (${RELEASE_ASSETS} assets)"
          fi
        env:
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
