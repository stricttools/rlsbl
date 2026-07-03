name: Publish

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

permissions:
  contents: write

jobs:
{{publishGate}}
  build-and-upload:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "mlugg/setup-zig"}}
        with:
          version: {{zig.minRequiredZig}}

      - name: Build x86_64-linux
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-x86_64-linux

      - name: Build aarch64-linux
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-linux
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-aarch64-linux

      - name: Build x86_64-macos
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-macos
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-x86_64-macos

      - name: Build aarch64-macos
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
          cp zig-out/bin/{{zig.projectName}} {{zig.projectName}}-aarch64-macos

      - name: Build x86_64-windows
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-windows
          cp zig-out/bin/{{zig.projectName}}.exe {{zig.projectName}}-x86_64-windows.exe

      - name: Build aarch64-windows
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-windows
          cp zig-out/bin/{{zig.projectName}}.exe {{zig.projectName}}-aarch64-windows.exe

      - name: Upload release assets
        run: |
          gh release upload "${{ github.ref_name }}" \
            {{zig.projectName}}-x86_64-linux \
            {{zig.projectName}}-aarch64-linux \
            {{zig.projectName}}-x86_64-macos \
            {{zig.projectName}}-aarch64-macos \
            {{zig.projectName}}-x86_64-windows.exe \
            {{zig.projectName}}-aarch64-windows.exe
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
{{npmPublishJobs}}
